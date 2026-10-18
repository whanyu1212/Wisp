---
name: wisp-development
description: Develop Wisp and its current extension surfaces while preserving architecture and safety boundaries.
license: MIT
compatibility: Wisp source checkouts and installed wisp-ai packages.
metadata:
  author: Wisp
  version: "1.5"
---

# Wisp Development

Use this skill when explaining, changing, or embedding Wisp. Establish whether the work targets a
source checkout or an installed package, then inspect the relevant implementation and tests before
proposing changes.

## Workflow

1. Identify the narrowest owning layer: agent loop, harness, coding session, RPC host, or frontend.
2. Preserve event-driven contracts and keep the Rust terminal UI a pure RPC client.
3. Treat project trust, protected paths, tool safety, and approvals as independent boundaries.
4. Use the existing runtime registration API instead of wiring capabilities into a frontend.
5. Update authoritative sources before generated artifacts, preserving stable catalog and schema
   ordering.
6. Add deterministic tests with fake or scripted providers and exercise real JSON round trips when
   events cross RPC.
7. Run the repository's documented Python, Rust, handoff, and packaging gates that match the change.

## Local evidence and delivery

This skill covers Wisp architecture, implementation, safety, and local verification. Create
branches, commits, pushes, pull requests, merges, or releases only when the user asks for that step.
In a Wisp source checkout, follow the delivery rules in `AGENTS.md`. Elsewhere, follow the project's
own instructions. Local checks show the change works on this machine. They do not show that remote
CI or review is clean for the commit being delivered, so report a pull request as ready only from the
results for its current head commit.

Load only the supporting resource needed for the task:

- `references/architecture.md` — layers, events, persistence, and frontend boundaries.
- `references/runtime-invariants.md` — compatibility contracts vs incidental internals for the agent loop and harness.
- `references/extension-api.md` — currently implemented extension registration and limitations.
- `references/safety.md` — trust, protected paths, approvals, and extension constraints.
- `references/authoring.md` — source-checkout and installed-package workflows.
- `references/verification.md` — deterministic tests and release-quality checks.

Do not claim that Wisp currently discovers or executes user/project Python extensions. Static
factories exist for built-in and embedding use; dynamic loading, lifecycle ownership, and reload are
future work.
