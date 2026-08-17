---
title: Interfaces
---

# Interfaces

TODO — adapt from `README.md` § "Interfaces".

Cover the four frontends and when to reach for each:

- CLI (print mode and interactive).
- TUI (`wisp tui`).
- JSONL RPC (`wisp --mode rpc`).
- In-process SDK.

Emphasise that all four drive the same command host — see
[Layer stack](../architecture/layers). The practical payoff to lead with: steering,
approvals, and cancellation behave identically on every surface, because none of
them reimplements that logic. See [Staying in sync](./staying-in-sync).
