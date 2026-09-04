---
name: wisp-development
description: Develop Wisp and its current extension surfaces while preserving architecture and safety boundaries.
license: MIT
compatibility: Wisp source checkouts and installed wisp-ai packages.
metadata:
  author: Wisp
  version: "1.2"
---

# Wisp Development

Use this skill when explaining, changing, or embedding Wisp. Establish whether the work targets a
source checkout or an installed package, then inspect the relevant implementation and tests before
proposing changes.

## Workflow

1. Identify the narrowest owning layer: agent loop, harness, coding session, RPC host, or frontend.
2. Preserve event-driven contracts and keep the Textual TUI a pure RPC client.
3. Treat project trust, protected paths, tool safety, and approvals as independent boundaries.
4. Use the existing runtime registration API instead of wiring capabilities into a frontend.
5. Add deterministic tests with fake or scripted providers and exercise real JSON round trips when
   events cross RPC.
6. Run the repository's documented format, lint, type, and test gates.

## GitHub delivery boundary

When Wisp development continues into branch, commit, push, pull-request, CI, review, or
merge-readiness work on GitHub, load the resolved `github-pr-delivery` entry and inspect its content
before using it alongside this one. The name alone does not prove that Wisp's package entry was
selected because a higher-precedence project or user entry may shadow it. Do not describe the resolved
entry as bundled or assume guarantees that its content does not establish. This skill owns Wisp
architecture, implementation, safety, and local verification; keep authorization-sensitive remote
mechanics in the applicable delivery workflow rather than copying them here. Local verification is an
input to PR delivery, not proof that remote CI or review is current and clean.

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
