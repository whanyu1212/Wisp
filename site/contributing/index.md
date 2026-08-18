---
title: Contributing
---

# Contributing

Wisp welcomes focused bug fixes, documentation improvements, tests, and features that preserve its
single typed runtime. Keep provider-specific behavior in provider adapters, in-memory run state in
`AgentHarness`, durable policy in `CodingSession`, and frontend behavior aligned through shared RPC
commands and `WispEvent` models.

Before opening a pull request, run the checks appropriate to your change and keep observable order
stable in prompts, tool schemas, replay items, events, and persisted entries. Changes to approvals,
protected paths, cancellation, retries, or process cleanup should include adversarial regression
coverage.

See [Development setup](./development) to prepare a checkout and [Testing](./testing) for local and
CI test partitions.
