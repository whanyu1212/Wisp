---
title: Layer stack
---

# Layer stack

TODO — adapt from `CLAUDE.md` § "The layer stack".

Each layer adds exactly one concern. Change code at the narrowest layer that can
express it.

Reproduce the layer table with, for each layer: module, what it owns, and what it
must not know about — `run_agent_loop`, `AgentHarness`, `CodingSession`, the RPC
command host, and the adapters.

Also cover:

- Why the pure loop depends only on the `Provider` and `ToolExecutor` protocols.
- Why there is exactly one agent loop, and why a per-interface loop is not an option.
