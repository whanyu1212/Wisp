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
CI test partitions. If you are changing distribution or installation behavior, read
[How Wisp is built](./building) before adjusting packaging. Start with the smallest reproducible
case, add a regression for the intended behavior, and include the checks you actually ran in the
pull request.

Report security vulnerabilities privately, not in public issues; the repository's
[security policy](https://github.com/whanyu1212/Wisp/blob/main/SECURITY.md) explains how and what is
in scope.
