---
title: Introduction
---

# Introduction

Wisp is a coding agent that **stays in sync with you**. Its fullscreen TUI, RPC, and SDK surfaces
can steer an active run or queue follow-up work, and every interface uses the same typed runtime
rather than a separate implementation.

The harness makes its work observable and recoverable: tool calls pass through explicit safety
gates, lifecycle events arrive in an enforced order, and sessions are append-only JSONL records
that can be inspected and resumed. These guarantees make Wisp useful both at a terminal and inside
long-lived integrations.

Wisp 0.1.0 is the first stable release. While Wisp remains below 1.0, later minor releases may make
announced breaking changes under the documented compatibility policy.

Start with [Installation](./installation) and the [Quickstart](./quickstart). Then read
[Staying in sync](./staying-in-sync) for the exact steering, follow-up, cancellation, and approval
behavior available through each interface. Embedders can continue with the [Python SDK](./sdk).
