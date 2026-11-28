---
title: Introduction
description: Personal terminal coding agent in Python and Rust with a daily-driver workspace, explicit safety gates, append-only sessions, and one shared runtime.
route: guide/index.html
section: using
order: 0
layout: docs
listed: true
searchable: true
navTitle: Using Wisp
---

# Introduction

Wisp is a personal terminal coding agent written in Python and Rust, built for daily repository
work and for tinkering with agent runtimes from the loop up.

In the terminal, it gives you a fast Rust workspace designed to stay in sync while you work:
inline diffs and turn-scoped `/diff` (`Alt+G`), a docked `Session | Files | Browser` side panel
with a live built-in browser and turn waterfall, local `!` shell runs you can selectively share
with `Alt+S`, and live steering or follow-up queues so you can redirect an active run without
cancelling it.

Underneath, the Rust TUI, one-shot print/JSON CLI, JSONL-RPC backend, and Python SDK all drive the
same typed Python runtime—with separate project trust and per-call tool approvals (including
saved command-prefix allow rules and macOS sandboxed Auto commands) and append-only JSONL sessions
you can inspect, resume, or branch at any time.

- **New to Wisp?** Follow [Installation](installation.md) and [Quickstart](quickstart.md), then
  skim [TUI basics](tui.md) for the workspace workflow.
- **Tuning daily use?** Read [Tools & safety](tools-and-safety.md), [MCP & codemode](mcp.md), and
  [Sessions](sessions.md).
- **Embedding or going deeper?** Try the [Python SDK](sdk.md), build a tool-using agent from
  scratch in [Crafting Coding Agents](../crafting-agents/index.md), or inspect runtime internals
  and evals under [Inside Wisp](../architecture/index.md).

This guide tracks current Wisp (`0.16.x`); see
[Compatibility & versioning](../reference/compatibility.md) and the
[changelog](https://github.com/whanyu1212/Wisp/blob/main/CHANGELOG.md) for release policy and
per-version notes.
