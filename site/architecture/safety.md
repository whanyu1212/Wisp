---
title: Safety model
---

# Safety model

TODO — adapt from `CLAUDE.md` § "Safety model".

Three independent gates:

1. **Project trust** — recorded globally at `~/.wisp/trust.json`, keyed by resolved
   absolute path, never read from the project directory. Undecided fails closed.
2. **Tool safety categories** — `read` / `mutating` / `command`, enforced outside
   the model's control.
3. **Protected paths** — glob patterns whose contents tools refuse to read.

Also cover the design rule for new project-writable config inputs: enumerate every
channel a project can write to, gate them all, require absolute paths for
security-critical files, and verify empirically rather than by reading code.
