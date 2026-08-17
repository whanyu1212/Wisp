---
title: Reference
---

# Reference

Exact surfaces — flags, fields, schemas. Narrative explanation lives in the
[Guide](../guide/); design rationale lives in [Architecture](../architecture/).

- [CLI](./cli) — commands, flags, exit codes.
- [Configuration](./configuration) — settings files and precedence.
- [Environment variables](./environment) — every `WISP_*` variable.
- [Events](./events) — the `WispEvent` catalogue and schema versioning.
- [RPC protocol](./rpc) — JSONL commands and responses.
- [SDK](./sdk) — the in-process Python API.

::: warning Keep in sync with code
These pages describe versioned surfaces. When `EVENT_SCHEMA_VERSION` or a settings
field changes, update the corresponding page in the same change.
:::
