---
name: wisp-code-review
description: Review Wisp changes for correctness, architecture, security boundaries, and test coverage.
license: MIT
compatibility: Designed for the Wisp repository and an agent with repository read access.
metadata:
  author: Wisp
  version: "1.0"
---

# Wisp Code Review

Review the requested Wisp change as a maintainer. Prioritize behavioral defects, regressions,
security boundary violations, and missing tests over style preferences.

## Workflow

1. Establish the review scope from the requested commit, branch, or working-tree diff.
2. Read the relevant implementation and tests before drawing conclusions.
3. Trace the change through Wisp's narrowest owning layer:
   `run_agent_loop` -> `AgentHarness` -> `CodingSession` -> RPC host -> frontend adapter.
4. Check serialized event and session changes for schema-version updates and JSON round trips.
5. Check project-controlled inputs against trust, protected-path, tool-policy, and approval gates.
6. Verify new behavior has deterministic tests at the boundary where the bug or contract lives.
7. Report only actionable findings that are supported by a concrete failure scenario.

When the read-only `skill` tool is available and the change warrants a deeper audit, load
`references/checklist.md` from this skill. The checklist is supplementary; do not fail the review
only because the resource cannot be loaded.

## Review Rules

- Do not modify files unless the user explicitly asks for fixes.
- Do not treat skill metadata as permission to run commands or mutate the repository.
- Do not recommend bypassing trust, protected paths, approvals, or tool safety categories.
- Avoid speculative abstractions and unrelated refactors.
- Confirm that tests exercise the real transport or UI boundary when serialization or Textual
  behavior is involved.

## Output

Lead with findings ordered by severity. Include file and line references, the triggering scenario,
and the user-visible consequence. Follow with open questions and testing gaps only when relevant.
If no findings remain, say so explicitly and identify any residual risk that was not verified.
