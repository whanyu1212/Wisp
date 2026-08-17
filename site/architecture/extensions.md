---
title: Extensions
---

# Extensions

TODO — adapt from `CLAUDE.md` § "Extensions / registration".

Cover:

- `ExtensionAPI` / `WispRuntime` in `runtime/api.py`.
- `build_runtime()` and how extensions are activated.
- Registering a provider, tool, or command through `extensions/builtin.py`.
- Why only built-in/static factories are supported today, and what project-local
  extension loading would require first.
