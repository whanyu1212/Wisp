# 2. Reading, editing, and testing code

Our agent can observe the broken addition function, but it cannot fix it. This
chapter gives the same loop three operations: read the source, replace a known
piece of text, and run the project's tests.

By the end, the trace will show a failing test before the edit and passing tests
afterward. We will also deny the edit and confirm that a finished run can leave
the task unresolved.

## 1. A tool has two contracts

The **model-facing description** says what operation exists and what arguments it
accepts. The **host-facing executor** decides how that operation runs. Keeping
these distinct lets us validate a request before giving it any effect.

Our teaching tools are deliberately narrow:

| Tool | Required string arguments | Meaning |
| --- | --- | --- |
| `read` | `path` | Read `calculator.py` |
| `edit` | `path`, `old`, `new` | Replace exactly one non-empty match in `calculator.py` |
| `test` | None | Run the fixture's fixed addition tests |

The host supplies the working directory and whether edits are approved. Neither
is a tool argument. A model cannot grant itself permission by placing
`"approved": true` in a request.

Three questions are easy to confuse:

- **Exposure:** was this tool described to the model?
- **Policy:** will the executor accept this operation and target?
- **Approval:** has the host authorized this effect?

Hiding a tool description is not an execution gate. The executor must still
reject unknown names and invalid arguments.

## 2. Build a fixture executor

The executable source is
[`checkpoint_02.py`](https://github.com/whanyu1212/Wisp/blob/main/examples/crafting_agents/checkpoint_02.py).
It imports the loop from chapter 1, creates `calculator.py` and two addition tests
inside a temporary directory, and removes that directory when finished.

The dispatcher looks up the tool, checks the exact argument names and string
values, and bounds both successful output and expected errors:

```python
{{#include ../../examples/crafting_agents/checkpoint_02.py:dispatch}}
```

An extra argument is rejected rather than silently ignored. That makes failures
actionable and keeps misspellings from turning into surprising behavior. We catch
expected file, decoding, and timeout failures here; an unexpected executor bug
still propagates.

After validation, the three operations are straightforward:

```python
{{#include ../../examples/crafting_agents/checkpoint_02.py:operations}}
```

### Why require exactly one match?

An edit is a claim about the file the model observed: “replace this text here.”
Zero matches means that claim is stale or incorrect. Multiple matches make the
location ambiguous. Refusing both outcomes makes the model reread or supply a
more specific edit instead of changing an arbitrary occurrence.

This simplicity has a cost: mechanical changes across many occurrences require
more calls. Later designs might offer patch-oriented or structured editing, but
they still need a policy for stale and ambiguous input.

### Why is a failed test a normal result?

The test process returning a nonzero exit code is expected information. Our tool
returns the exit code and diagnostics; it does not crash the agent because the
bug has been reproduced. A process that cannot start or exceeds its deadline
instead produces an operational error observation.

The fixed command uses the current Python interpreter and no shell. That keeps
this checkpoint easy to inspect. It is not a general-purpose command runner.

### Bound what the next model request receives

Line and byte budgets protect different cases: many short lines versus a single
very long line. Apply both, even after the first limit has truncated the output:

```python
{{#include ../../examples/crafting_agents/checkpoint_02.py:budget}}
```

The result stores truncation separately from the bounded text. Our dispatcher
renders it as a small `truncated=true/false` header; the byte and line caps apply
to the body, with the fixed header additional. Cutting encoded bytes and decoding
with `errors="ignore"` avoids returning half of a UTF-8 character.

This limits model-visible output **after capture**. It does not bound memory while
a process runs. The fixture produces small, known output; a general shell tool
needs bounded capture during execution, which the [tool-boundary case study](case-studies/tool-boundary.md)
examines.

## 3. Inspect a complete repair

From the checkout root:

```bash
python3 -m examples.crafting_agents.checkpoint_02
```

The trace includes test diagnostics; its key transitions are:

```text
turn 1: Reproduce the bug.
... test returns exit_code=1 and FAILED ...
turn 2: Read the implementation.
... read returns return a - b ...
turn 3: Replace subtraction with addition.
... edit returns edited calculator.py ...
turn 4: Check the change.
... test returns exit_code=0 and OK ...
turn 5: The two fixture tests pass after the edit.
stopped: model_finished
final calculator.py:
def add(a, b):
    return a + b
```

The provider is still scripted. Its `after` checks require the expected failure,
source, edit acknowledgment, and passing test output before it advances. We have
verified a repair workflow and two concrete test cases, not measured a model's
ability to find a fix or proven correctness for every possible input.

## 4. Deny the edit

Run the second scenario:

```bash
python3 -m examples.crafting_agents.checkpoint_02 --deny-edits
```

The initial test still fails and the source is still read. The edit returns
`error: edit denied by the host`, and the last response says `Edit denied; the bug
remains.` The final source still contains `return a - b`.

The flag chooses both a host permission and a matching scripted conversation. A
live model would receive the denial and decide how to respond; this script only
demonstrates the host's observable behavior.

### What this boundary does not yet solve

The example assumes a trusted, disposable fixture with no concurrent writers.
Its path allowlist and symlink check do not provide race-resistant filesystem
isolation. Writes are not atomic. The test runner executes fixture code with the
Python process's privileges, captures output in memory, and blocks until it ends
or times out. It has no process-tree supervision or interactive cancellation.

These are reasons to keep the checkpoint scoped to its fixture. Chapter 5 will
develop the stronger execution boundary. For Wisp's current mechanisms, read
[Hardening the tool boundary](case-studies/tool-boundary.md).

## 5. Wisp's choice: a small tool surface with richer contracts

Wisp's local built-ins are `read`, `write`, `edit`, `bash`, `grep`, `find`, and `ls`.
Skills and MCP can expose additional tools through their own integration paths.
Each local tool has a schema, executor, and safety category.

| Teaching mechanism | Wisp's corresponding choice |
| --- | --- |
| Required string arguments | Provider-facing JSON schemas plus runtime argument validation |
| Host-owned fixture directory | `ToolContext` with working directory, budgets, protected paths, and write scopes |
| `approve_edits` flag | Separate exposure, `ToolPolicy`, and `ToolApprovalPolicy`, with typed approval events |
| Error observation | `ToolError` with failure code, retryability, and recovery hint |
| Output prefix and flag | `ToolResult` with model text, structured data, and truncation state |
| Sequential execution | Validated tool lifecycles and prepared batches with controlled parallelism |

Wisp's exact-match edit follows the same basic reasoning as our example, while
also checking concurrent changes and securing file access. A stale edit can tell
the model to reread the range rather than offering only “edit failed.”

The richer contract costs more implementation and testing. Wisp validates event
ordering, detaches mutable argument payloads, and separates preparing approvals
from performing side effects. Those costs buy consistent behavior across the
TUI, SDK, and RPC clients. A fixed, trusted batch script may not need an
interactive approval lifecycle; a developer-facing agent does.

Tools also compete for model attention and context. A small surface is easier
to describe and audit, but Wisp's exact-match edits and unranked search can require
more interactions than specialized operations. Tool design should be evaluated
against actual tasks, not just the number of tools offered.

### Follow the implementation

- [`tools/base.py`](https://github.com/whanyu1212/Wisp/blob/main/src/wisp/tools/base.py)
  and [`tools/result.py`](https://github.com/whanyu1212/Wisp/blob/main/src/wisp/tools/result.py):
  tool and result contracts.
- [`tools/files/operations.py`](https://github.com/whanyu1212/Wisp/blob/main/src/wisp/tools/files/operations.py):
  the read, write, and edit implementations.
- [`loop/prepared_tools.py`](https://github.com/whanyu1212/Wisp/blob/main/src/wisp/agent/loop/prepared_tools.py):
  approval preparation and execution scheduling.
- [`test_tool_execution.py`](https://github.com/whanyu1212/Wisp/blob/main/tests/coding/test_tool_execution.py):
  executor behavior and lifecycle coverage.

For performance decisions, continue to [Earning a Rust boundary](case-studies/rust-boundary.md).
It explains why Wisp moved narrow scanning and output-retention kernels into Rust
while keeping tool policy and orchestration in Python.

## 6. Checkpoint

**Exercise 1: stale edits.** Change the script's `old` text to something absent
from the fixture. Expect an error, unchanged source, and a failed script
expectation instead of a false success. Extend the script with a reread and a
corrected edit before running the tests again.

**Exercise 2: both output limits.** Call `bound_output` on `"é" * 100 + "\nx\ny\n"`
with `max_bytes=9` and `max_lines=2`. The result must be valid UTF-8, at most nine
bytes, at most two lines, and marked truncated. Check an input below both limits
as well: it must be returned unchanged with `truncated=False`.

The regression tests for these teaching contracts live in
[`tests/docs/crafting/test_agents.py`](https://github.com/whanyu1212/Wisp/blob/main/tests/docs/crafting/test_agents.py).
From a development checkout, run `uv run pytest tests/docs/crafting/test_agents.py`.

Next: [Giving the model useful context](03-context.md). The loop can now execute
a repair; the next problem is choosing the
instructions and repository information a real model needs to decide what to do.
