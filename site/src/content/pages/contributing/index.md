---
title: Contributing
description: Project scope, bug reporting policy, local development setup, testing, and packaging notes for Wisp.
route: contributing/index.html
section: contributing
order: 0
layout: docs
listed: true
searchable: true
---

# Contributing

Wisp is personal software built for daily terminal use and to explore agent engineering. The source
and documentation are MIT-licensed to read, learn from, or fork, rather than run as an
open-contribution product:

- **Bug reports:** GitHub issues are open for reproducible bug reports.
- **Feature requests & pull requests:** Feature requests and unsolicited pull requests aren't being
  sought.

The pages in this section record how the repository is developed, verified, and packaged—keeping
provider-specific behavior in provider adapters, in-memory run state in `AgentHarness`, durable
policy in `CodingSession`, and frontend behavior aligned through shared RPC commands and
`WispEvent` models.

When verifying changes locally (in a fork or for an agreed-upon bug fix), run the checks appropriate
to the area you touched and keep observable order stable in prompts, tool schemas, replay items,
events, and persisted entries. Changes to approvals, protected paths, cancellation, retries, or
process cleanup require adversarial regression coverage.

See [Development setup](development.md) to prepare a checkout and [Testing](testing.md) for local and
CI test partitions. If you are inspecting distribution or installation behavior, read
[How Wisp is built](building.md) for how native and pure wheels are assembled.

Report security vulnerabilities privately, not in public issues; the repository's
[security policy](https://github.com/whanyu1212/Wisp/blob/main/SECURITY.md) explains how and what is
in scope.
