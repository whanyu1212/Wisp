---
title: Event model
---

# Event model

TODO — adapt from `CLAUDE.md` § "Events are the contract".

Cover:

- Why frontends render events rather than calling into core logic.
- Schema versioning discipline and the named breadcrumb constants.
- The two independent paths by which events reach disk.
- The enforced provider stream ordering, worth drawing as a mermaid diagram:
  `ProviderRetrying*` → `ProviderResponseStarted` → deltas → exactly one terminal.
- Why narrow typed fields are preferred over opaque payloads.

See [Events reference](../reference/events) for the catalogue itself.
