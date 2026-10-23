---
title: Introduction
description: Wisp is a coding agent that **stays in sync with you**. Its fullscreen TUI, RPC, and SDK surfaces can steer an active run or queue follow-up work, and every interface uses the…
route: guide/index.html
section: using
order: 9
layout: docs
listed: true
searchable: true
navTitle: Using Wisp
---

# Introduction

Wisp is a coding agent that **stays in sync with you**. Its fullscreen TUI, RPC, and SDK surfaces
can steer an active run or queue follow-up work, and every interface uses the same typed runtime
rather than a separate implementation.

The harness makes its work observable and recoverable: tool calls pass through explicit safety
gates, lifecycle events arrive in an enforced order, and sessions are append-only JSONL records
that can be inspected and resumed. These guarantees make Wisp useful both at a terminal and inside
long-lived integrations.

This guide describes Wisp 0.2. While Wisp remains below 1.0, a minor release may make announced
breaking changes under the documented compatibility policy; the [upgrade guide](upgrading.md) lists
the ones in 0.2.

Start with [Installation](installation.md) and the [Quickstart](quickstart.md). Then read
[Staying in sync](staying-in-sync.md) for the exact steering, follow-up, cancellation, and approval
behavior available through each interface. Embedders can continue with the [Python SDK](sdk.md).

For daily terminal work, the [TUI guide](tui.md) covers composing prompts, reviewing changes, and
finding commands. Read [Tools and safety](tools-and-safety.md) before enabling unattended execution,
and [Sessions](sessions.md) when you want to resume, branch, or inspect saved work. The section
navigation lists the rest of the guide in reading order.
