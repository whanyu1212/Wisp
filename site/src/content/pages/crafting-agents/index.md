---
title: Crafting Coding Agents
description: Learn the engineering decisions behind building a coding agent and examine Wisp as a concrete production case study.
route: crafting-agents/index.html
section: learn
order: 0
layout: course
listed: true
searchable: true
navTitle: Course overview
---

# Crafting Coding Agents

*Learn the engineering decisions involved in building a coding agent, then examine
Wisp as a concrete, evolving answer to those decisions.*

A model can suggest a fix. A coding agent must find the relevant code, make a
change, check it, and keep the developer informed and in control. Doing that well
requires more than a model API and a shell command: someone must own the
conversation, bound tool output, handle interruption, and decide what survives a
restart.

This book builds those mechanisms one at a time. Wisp is our production case
study. Its choices have benefits and costs; they are examples to reason about,
not requirements for every agent you build.

## Who this is for

You should be comfortable with Python functions, dataclasses, exceptions, and
basic `async`/`await`. You do not need to know Wisp or any agent framework.
We introduce model/tool vocabulary before using it.

If you want to run Wisp rather than build an agent, start with the
[quickstart](../guide/quickstart.md). The [architecture guide](../architecture/agent-runtime.md)
is a companion for navigating Wisp's current source.

## One agent, one continuing task

Our running task is small: **fix a broken addition function in a fixture project**.
The small bug lets us inspect the whole interaction instead of spending a chapter
understanding the application being edited.

We begin with an in-memory file and a model/tool loop. Next we give that same loop
a disposable directory, an exact-match editor, and a test runner. Chapter 3 adds
request context and on-demand file discovery. Chapter 4 introduces streaming,
failure replays, and an opt-in live provider. Chapter 5 closes **Part I** by separating
host policy, per-operation approval, and filesystem/process guarantees. Each layer
solves a problem the previous version makes visible.

The first three checkpoints use a **scripted provider**: a fixed sequence of model
decisions with checks on the observations between them. This makes the examples
repeatable and runnable without credentials. It demonstrates execution mechanics,
not a model discovering a solution or evidence of coding ability. Chapter 4 adds
authored provider-event replays plus an optional live adapter; the offline path
remains the default.

### Run the available checkpoints

From a source checkout with Python 3.11 or newer:

```bash
python3 -m examples.crafting_agents.checkpoint_01
python3 -m examples.crafting_agents.checkpoint_02
python3 -m examples.crafting_agents.checkpoint_02 --deny-edits
python3 -m examples.crafting_agents.checkpoint_03
python3 -m examples.crafting_agents.checkpoint_04
python3 -m examples.crafting_agents.checkpoint_05
python3 -m examples.crafting_agents.checkpoint_05 --scenario stale-input
```

These commands use only the Python standard library. Chapter 1 reads an in-memory
fixture. Chapter 2 creates and removes a temporary directory; it does not edit
your checkout. Chapters 3–5 extend that fixture with context, discovery, stream
handling, and host-side approval boundaries (the final `--scenario stale-input`
experiment shows how the host blocks an approved edit when the file changes
during approval). Their test tool executes the supplied fixture with the current
Python interpreter. The separate live command in chapter 4 requires the OpenAI
SDK and credentials; live edits and test execution require an explicit opt-in.

## How to read a chapter

Each chapter follows the same progression:

1. **Encounter a problem.** What can our agent not yet do reliably?
2. **Build the mechanism.** Add a small, runnable capability to the teaching agent.
3. **Inspect the trace.** See the requests, observations, and resulting state.
4. **Break an assumption.** Try a failure with an observable outcome.
5. **Study Wisp's choice.** Connect the mechanism to implementation entry points,
   costs, alternatives, and tests.
6. **Complete a checkpoint.** Make a focused change and verify its behavior.

Chapter code listings are included from the runnable sources so the book and
examples share the same implementation. Pages that explain Wisp's own source, such
as [Inside Wisp's built-in tools](02-tools/built-in-tools.md), use shortened
excerpts instead and say so at the top. The teaching agent is intentionally smaller than
Wisp; each chapter names the guarantees it has yet to earn.

## Part I — Build a tool-using agent {#curriculum}

Part I is a complete, five-chapter arc that takes a coding agent from a single-file
loop to controlled, verified side effects:

| Chapter | What you build | Wisp connection |
| --- | --- | --- |
| [1. The smallest coding agent](01-core-loop.md) | The model/action/observation cycle, correlated tool results, explicit stopping | `run_agent_loop` |
| [2. Reading, editing, and testing code](02-tools.md) | Validated tool dispatch, exact-match edits, test feedback, output limits (companion: [Inside Wisp's built-in tools](02-tools/built-in-tools.md)) | Built-in tools and typed results |
| [3. Giving the model useful context](03-context.md) | Instruction assembly, repository discovery, selecting relevant information | Prompt builder, project context, skills |
| [4. Talking to models reliably](04-providers.md) | A live provider adapter, streaming, completion signals, safe retries | Provider adapters and lifecycle validation |
| [5. Controlling side effects](05-side-effects.md) | Exposure, policy, approval, trust, filesystem and process boundaries | Tool policies, secure files, process supervisor |

## Wisp case studies

After Part I—or alongside Chapter 2 and Chapter 5—these case studies examine how
Wisp handles production boundaries and performance trade-offs:

- [Hardening the tool boundary](case-studies/tool-boundary.md): file races,
  process lifetime, search, scheduling, and current limitations.
- [Earning a Rust boundary](case-studies/rust-boundary.md): benchmarks, profiling,
  narrow native kernels, and the cost of maintaining parity.

Future **Part II** chapters are scoped around runtime control and durability:
live steering and cancellation (`AgentHarness`), append-only JSONL sessions and
replay (`CodingSession`), context compaction, multi-interface RPC, and task
evaluation. To inspect how Wisp implements and verifies those layers today, read
[Agent runtime](../architecture/agent-runtime.md),
[Rust TUI boundary](../architecture/rust-tui-boundary.md), and
[Evals and reliability](../architecture/evals-and-reliability.md).

Begin with [Chapter 1: The smallest coding agent](01-core-loop.md).
