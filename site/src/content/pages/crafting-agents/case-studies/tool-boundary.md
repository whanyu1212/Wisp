---
title: 'Case study: Hardening the tool boundary'
description: Why Wisp's built-in tools guard against file races, runaway processes, and unbounded search, what those protections cost, and what remains open.
route: crafting-agents/case-studies/tool-boundary.html
section: learn
order: 6
layout: course
listed: true
searchable: true
navTitle: Hardening the tool boundary
navGroup: Case studies
---

# Case study: Hardening the tool boundary

The [chapter 2 checkpoint](../02-tools.md) assumes a small trusted fixture and a
single writer. Wisp operates inside developer repositories, runs long-lived
commands, and accepts calls from models and custom executors. This case study
explains which of those pressures shaped its built-in tools, what each response
costs, and what remains unsolved.

It deliberately leaves out _how_ the tools work. For the step-by-step mechanics,
with code, read [Inside Wisp's built-in tools](../02-tools/built-in-tools.md);
the links below point to the relevant sections.

## Files: a path check is not enough

**The pressure.** The teaching tools check a path string, then open it. In a real
repository, the file system can change in between: a directory can be replaced by a
symbolic link that leads outside the project, or the user, a formatter, or another
process can rewrite the file the model is about to change. A check on a string says
nothing about what the later `open` reaches.

**The response.** Wisp treats the check and the open as one operation: every path
component is opened relative to the directory before it, without following links,
so a swapped-in link makes the open fail rather than escape. Changes require the
file version the model last read, and are published by an atomic rename. See
[From a path string to an open file](../02-tools/built-in-tools.md#2-from-a-path-string-to-an-open-file)
and [`write`](../02-tools/built-in-tools.md#4-write).

**The cost.** This needs platform primitives (`openat`, `O_NOFOLLOW`, directory
descriptors) and a separate Windows path. Where they are missing, Wisp denies
access instead of falling back to plain path opens. The read-before-write rule also
costs the model an extra `read` before editing a file it already knows.

“Atomic write” is not a universal guarantee here. When the destination has
multiple hard links, or a permission fallback requires it, Wisp writes in place
to preserve the inode. Other readers may observe a partial write, and hard-link
aliases see the change. That is a meaningful compatibility trade-off to document,
not hide behind an atomic-write label.

## Shell: output and lifetime need owners

**The pressure.** The chapter's `subprocess.run(..., capture_output=True)` holds all
output in memory and only truncates it afterwards, and it waits only for the
process it started. A real command can print gigabytes, start a background server
that outlives the shell, or detach completely into its own session. Each of these
can exhaust memory or keep consuming resources after the tool call returns.

**The response.** One `ProcessSupervisor` owns every process a command starts, not
just the first one. It keeps a bounded tail of output as it arrives, stops the whole
process tree when a call ends, and reports results that distinguish “still
running,” “finished,” and “I saw only part of its log.” When a result shows only
part of the log, the full streams are saved, within fixed limits, for `read_output`
to page through, so the model does not rerun a command to see its beginning. See
[`bash`](../02-tools/built-in-tools.md#6-bash). These concerns live beside the
supervisor rather than in each frontend, so the TUI, SDK, and RPC clients cannot
disagree about them.

**The cost.** Finding every process a command started is platform-specific work:
process groups, a Linux subreaper, a recorded list of background jobs, an
environment token, and a watchdog process for the case where the backend dies.
Cleanup can still fail to account for a process, and then the call fails rather
than claiming success. Stopping background processes at the end of `run` also
surprises users who expected `npm run dev &` to keep running; they must use
`operation=start` instead.

The output-retention kernel is partly native; [the Rust case study](rust-boundary.md)
explains the measurement behind that boundary.

## Search: two engines, one policy

**The pressure.** A read-only tool can still do damage. A recursive search of an
untrusted repository can meet huge directories, enormous ignore files, binary
input, minified one-line bundles, and regular expressions that take exponential
time. Without limits, a “safe” `grep` can exhaust memory or hold the turn
indefinitely, and it can print secrets that a `read` would refuse.

**The response.** `grep` and `find` share one secured walk. It applies the file
tools' link and protected-path rules plus the repository's ignore rules, with
explicit caps on directory size, depth, ignore files, line length, regex time, and
total duration.
See [`grep`](../02-tools/built-in-tools.md#7-grep).

**The cost.** Results are capped and in path order, not ranked, so the most relevant
match can be cut off. The caps also reject some legitimate inputs: a minified
file is skipped, and a directory with more than 100,000 entries fails the search.
Using the optional Rust scanner and walk adds a second implementation that must
stay exactly equivalent. Wisp keeps the policy in one
place for that reason: protected files are skipped during the walk, before either
engine reads them. An acceleration path must not become a different permissions
path.

## Execution: prepare approvals before side effects

Wisp separates `ToolPolicy` (“may this operation run?”) from `ToolApprovalPolicy`
(“must the user confirm it?”), and keeps working directory, budgets, protected
paths, and write scopes in a host-owned `ToolContext`. Model arguments cannot grant
approval or change a tool's safety category. The order of those checks is shown in
[What happens before `run`](../02-tools/built-in-tools.md#what-happens-before-run). This section covers
what happens around them: checking that executors behave, and scheduling a batch of
calls.

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
