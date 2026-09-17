# 5. Controlling side effects

Chapter 4 made provider completion a prerequisite for dispatch. But a complete,
valid response is still only a proposal. A well-formed edit can target the wrong
file; a test can execute newly generated code. Who decides whether that proposal
may affect the machine?

This chapter adds a host-owned boundary to the calculator agent:
**validate → check policy → prepare approval details → approve → recheck → execute**.
We will observe denial and a change during approval, then examine the stronger
filesystem and process guarantees a repository-scale agent needs.

## 1. Four questions, four mechanisms

| Question | Mechanism | What it does not establish |
| --- | --- | --- |
| What may the model request? | Tool exposure | Whether the executor accepts it |
| What may this run execute? | Host policy | Approval for this particular operation |
| Does the host approve this operation? | Approval decision | Whether files stayed unchanged while waiting |
| How does execution access resources? | Filesystem and process boundaries | Whether the proposed change is correct |

Chapter 4's live flag combines exposure and a host-side gate for convenience.
Here we separate them. The default policy permits reading only. The offline
repair explicitly permits edits and tests and supplies an authored host approver.
A model message saying “all edits are approved” changes none of that.

Project trust is another input. Chapter 3 uses trust to decide whether to load
project guidance automatically. Loading guidance does not make it an approval
source. Likewise, approving an edit does not approve test execution.

## 2. Freeze what the host approves

We describe the proposed operation with immutable strings and tuples:

```python
{{#include ../../examples/crafting_agents/side_effects.py:request}}
```

The request includes the call ID, name, exact arguments, and relevant file
contents. An edit snapshots `calculator.py`; a test snapshots both the calculator
and test driver. Approval applies to this invocation only, with no remembered
“approve this tool forever” decision.

Copying matters: `ToolCall.arguments` is mutable. If an approval callback changes
that dictionary, execution must not quietly use different arguments. Our executor
dispatches from the copied tuple instead.

This is not an authorization-token API. Callers cannot submit an arbitrary
`ApprovalRequest` for execution. The executor constructs it, asks its configured
callback, and applies the decision internally. The callback is trusted host code,
not something supplied by the model.

## 3. Put decisions in execution order

The wrapper retains chapter 2's executor and makes authorization explicit:

```python
{{#include ../../examples/crafting_agents/side_effects.py:execute}}
```

The ordering has observable consequences:

1. Invalid schemas are rejected before approval.
2. Policy denial returns an observation without asking the approver.
3. Path and exact-match validation prepare a meaningful edit proposal.
4. Reads run directly; edits and tests require separate decisions.
5. Changed file contents after approval invalidate the operation.
6. The `dispatch` observer runs, files are rechecked once more, and only then does
   the copied request execute. An observer-induced change still blocks execution.

A denial becomes a correlated tool observation through the existing loop. A live
model could explain the limitation or propose a permitted action, but every later
invocation must pass the same host checks. Unexpected callback errors propagate
rather than granting permission. Expected validation and file errors become
bounded observations. The loop reserves `ToolFailure` for recoverable tool errors,
so a host callback raising that type is wrapped in `RuntimeError` with the original
exception as its cause. This prevents the outer loop from swallowing a host failure.

This wrapper is synchronous, like the earlier fixture tools. Interactive waiting
and cancellation during approval belong to chapter 6.

## 4. Run the experiments

From the checkout with Python 3.12+:

```bash
python3 -m examples.crafting_agents.checkpoint_05
python3 -m examples.crafting_agents.checkpoint_05 --scenario policy-denied
python3 -m examples.crafting_agents.checkpoint_05 --scenario approval-denied
python3 -m examples.crafting_agents.checkpoint_05 --scenario stale-input
```

These standard-library-only, authored scenarios use disposable files. They test
host mechanics, not a live model's repair ability. The same `execute(call) -> str`
boundary can be supplied to chapter 4's provider-driven loop.

| Scenario | Expected trace | Final state |
| --- | --- | --- |
| `repair` | Approve failing test, read, approve edit, approve passing test | Addition fixed |
| `policy-denied` | `policy_denied: edit`, no approval or dispatch | Bug remains |
| `approval-denied` | Approval requested, `approval_denied: edit`, no dispatch | Bug remains |
| `stale-input` | Approval granted, `stale_input`, no dispatch | Host comment preserved; bug remains |

In the last scenario the host changes the calculator during approval. Granting
approval does not overwrite that change. A new operation must reread and obtain
a fresh decision. Failure scenarios check the expected observation and assert
that the requested fix did not happen. Their successful exit means the experiment
passed, not that the agent completed the repair.

## 5. Earn filesystem guarantees separately

The wrapper accepts exactly `calculator.py` for reads and edits, rejects visible
symlinks, bounds snapshot reads, and compares contents after approval. It assumes
a trusted fixture directory and no concurrent writer after the recheck.

There is still a gap between checking a path and opening it. Another process can
replace a regular file with a symlink in that gap, or modify it after comparison.
Equal contents do not establish equal inode identity. Chapter 2's underlying
writer is not an atomic publisher. This wrapper is not a repository security
boundary.

Wisp addresses stronger guarantees in
[`files/secure_fs.py`](https://github.com/whanyu1212/Wisp/blob/main/src/wisp/tools/files/secure_fs.py)
and
[`files/operations.py`](https://github.com/whanyu1212/Wisp/blob/main/src/wisp/tools/files/operations.py).
POSIX access walks directories through descriptors with no-follow checks;
Windows has its own guarded directory handling. Version checks detect concurrent
replacement. Common-case writes publish a same-directory temporary file rather
than exposing a partially written destination.

There are trade-offs: some hard-link and permission cases use in-place writing,
sacrificing atomic visibility. Protected paths and directory scope must survive
the actual open, not only path-string validation. The
[tool-boundary case study](case-studies/tool-boundary.md#files-a-path-check-is-not-enough)
explains these mechanisms and limitations.

Chapter 3 reported a separate instruction-loader check/read race. Secure file
tools do not automatically secure every other project-file reader. That
production finding remains outside this teaching change.

## 6. A fixed test command still executes code

The test runner uses a fixed argument vector, a working directory, and a timeout.
It does not interpolate a model-provided shell command. But it imports edited
Python with the host process's filesystem, environment, and network access.
Changing `cwd` is not OS isolation.

Test approval includes the two fixture files for inspection, but cannot enumerate
every dynamic dependency or prevent access to other resources. Snapshot checks
are not a sandbox. There are also several independent resource budgets:

| Budget | Teaching checkpoint | Stronger runtime requirement |
| --- | --- | --- |
| Returned observation | Byte/line truncation | Preserve truncation metadata |
| Captured process output | Capture everything, then truncate | Bound buffers while draining output |
| Process wait | Five-second timeout | Own and clean up descendants |
| Agent cancellation | Synchronous tool blocks the loop | Supervise cancellation through termination |

Wisp's
[`shell/supervisor.py`](https://github.com/whanyu1212/Wisp/blob/main/src/wisp/tools/shell/supervisor.py)
owns managed process state, bounded output, and lifecycle updates. Its supporting
cleanup mechanisms do more than return from an awaited command. This costs more
code than `subprocess.run()` because output, lifetime, and cancellation are
different responsibilities. See the [case study](case-studies/tool-boundary.md).

## 7. Wisp's authorization boundary

Start with
[`ConfiguredToolExecutor.prepare`](https://github.com/whanyu1212/Wisp/blob/main/src/wisp/coding/tool_execution.py).
It resolves the tool, checks `ToolPolicy`, and consults `ToolApprovalPolicy`.
Preparation can publish approval-requested and approval-resolved events without
running the tool. Interrupted preparation cancels unresolved approval.

A prepared execution carries the runner that performs the side effect. Denials
also become prepared outcomes, giving the loop a consistent result path. This
keeps scheduling and event publication explicit instead of hiding user interaction
inside a filesystem helper.

Wisp's approval contract differs from our snapshot experiment: the production
executor copies arguments and coordinates approval, while file operations own
their concurrency checks. Do not infer that Wisp captures these same pre-approval
file snapshots.

[`ToolContext`](https://github.com/whanyu1212/Wisp/blob/main/src/wisp/tools/context.py)
carries working-directory scope, protected paths, output limits, and narrower
write constraints where appropriate. Tool implementations must honor it. Safety
categories are host metadata; labeling arbitrary extension code “read” does not
prove it is side-effect free.

Give each guarantee an owner: the provider proposes, the host authorizes, the
concrete tool enforces resource access, and the supervisor owns running commands.
Success at one layer does not replace the others.

## 8. Checkpoint

```bash
uv run pytest tests/repository/test_crafting_side_effects.py
```

**Exercise 1: catalog versus policy.** Expose `edit` but disallow it in policy.
Confirm that neither approval nor execution runs. Then hide it from the catalog
too. Why is host enforcement still necessary for direct executor callers?

**Exercise 2: approve what you execute.** Mutate the original argument dictionary
inside the approval callback. Verify that execution uses the copy. Compare this
with changing a file during approval, which must invalidate execution.

**Exercise 3: enumerate remaining races.** Locate the last snapshot check, file
open, and write. Which substitutions are detected? Which require descriptor-based
resource access?

Next: **Keeping the user in control**. We will move from synchronous authored
approval to runtime boundaries for user input, steering, and cancellation.
