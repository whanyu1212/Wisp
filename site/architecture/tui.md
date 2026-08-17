---
title: TUI internals
---

# TUI internals

TODO — adapt from `CLAUDE.md` § "TUI".

The Textual frontend is a **pure RPC client** — it spawns `python -m wisp --mode rpc`
as a subprocess and renders the events it emits. It holds no agent logic.

Cover:

- The controller ownership tree and the rule that controllers must not import the
  shell, RPC, provider, session, or agent runtime.
- The subprocess boundary: every event crosses a JSON round-trip, so anything the
  renderer needs must survive `model_dump_json()` → `wisp_event_from_json()`.
- Why untrusted payloads stay in escaped `Static`, never the Markdown parser.
- The Textual-specific traps worth writing down for contributors.
