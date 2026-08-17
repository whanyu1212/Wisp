---
title: Events
---

# Events

TODO — the event catalogue, generated from or checked against `events.py`.

Cover:

- Current `EVENT_SCHEMA_VERSION` and the named breadcrumb constants.
- Each `WispEvent` subtype with its fields.
- The strict provider stream ordering contract.
- Session versioning: `SESSION_ENTRY_SCHEMA_VERSION`,
  `PERSISTED_EVENT_ENVELOPE_SCHEMA_VERSION`, and the rejection path.

::: tip Single-version parsing
Schema versioning is strict single-version — the parser accepts only the current
version. A bump *moves* the accepted version rather than widening a range.
:::
