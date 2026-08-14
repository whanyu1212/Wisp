---
name: github-pr-delivery
description: Deliver focused changes through a GitHub pull request with verified commits, current-head CI, thread-aware review, and explicit authorization boundaries.
license: MIT
compatibility: Git repositories hosted on GitHub; adapts to the GitHub tools and CLI available in the active Wisp runtime.
metadata:
  author: Wisp
  version: "1.0"
---

# GitHub PR Delivery

Use this skill for end-to-end PR delivery, PR handoff, merge-readiness, or requests to iterate until
CI and review are clean. For a status-only request, remain read-only. For isolated issue management,
one-off CI diagnosis, or a single review comment, prefer a narrower GitHub workflow when available.

## Establish the delivery contract

Treat the user's requested boundary as the authorization boundary. Track separate authorization for:

- creating a branch, commit, push, or PR;
- observing current-head CI to a terminal result;
- repairing branch-owned CI failures;
- addressing feedback, replying, and resolving addressed threads;
- requesting and iterating automated re-review;
- merging or performing post-merge actions.

Never infer permission to merge, release, deploy, rewrite history, delete branches, or clean unrelated
files. Tool approval and authentication do not replace user authorization. Once a narrow phase is
authorized, retain it across in-scope retries and pushes, but do not expand it to materially new
effects.

State the terminal condition: PR creation, terminal CI, clean exact-head review, or merge. A skill is
an active workflow, not a durable background service; do not promise work after the active run ends.

## Preserve a checkpoint

Track repository, PR URL and number, base and head branches, current head SHA, latest push, required
checks, latest review trigger and reviewed SHA, and unresolved non-outdated actionable thread IDs.
After interruption or compaction, refresh remote head, CI, review, and thread state before continuing.
Never resume from stale conclusions.

## Adapt to available capabilities

Inspect the tools and skills actually exposed in the active runtime. Use a GitHub specialist skill or
structured connector when available; otherwise use authenticated `gh` and local `git`. Do not assume
that a named third-party skill, connector, GraphQL helper, or CLI extension is installed. If required
state cannot be retrieved with available capabilities, report the limitation instead of weakening the
readiness criteria.

Follow repository instructions and commit conventions. If active higher-priority instructions require
a commit trailer or attribution, preserve it exactly. Never add generic tool branding on your own.

## Delivery workflow

1. **Preflight:** inspect instructions, branch, remotes, worktrees, status, diff, recent commits, and
   relevant implementation/tests. Identify task-owned files and preserve unrelated work. Fetch the
   target remote before ancestry-sensitive claims and verify ancestry, authentication, and the base.
2. **Verify:** run focused tests and repository-required gates proportionate to the change. Distinguish
   task failures from pre-existing or external failures; never call an unrun, timed-out, or pending
   check successful.
3. **Package narrowly:** stage explicit task paths, inspect the staged diff and names, run a whitespace
   check, scan for secrets and debris, commit without replacing user identity, and push without force.
4. **Create or refresh the PR:** describe the net diff, behavior, compatibility/safety properties,
   exact verification, and known limitations. Default to ready-for-review unless the user or repository
   requires a draft.
5. **Drive current-head CI:** record the pushed SHA, wait for all required checks to become terminal,
   inspect failures, and—only when authorized—apply narrow branch-owned fixes before repeating from the
   new head.
6. **Process review thread-aware:** inspect comments, formal reviews, and review threads including
   resolution and outdated state. Verify findings, fix valid ones narrowly, push before claiming a fix,
   reply with evidence, and resolve only the addressed thread.
7. **Run exact-head re-review only when authorized:** tie each trigger and verdict to a recorded head.
   A queued reaction, silence, stale review, or top-level clean comment with actionable unresolved
   threads is not a clean result.
8. **Verify final readiness:** refresh PR metadata and separately report CI, review, thread state,
   mergeability, and caveats. Merge only when explicitly requested, then verify the merged state.

Load only the reference needed for the active phase:

- `references/preflight-and-packaging.md` — worktree safety, ancestry, verification, commits, and PR body.
- `references/ci.md` — exact-head CI polling, diagnosis, fixes, and bounded reruns.
- `references/review.md` — thread-aware feedback and exact-head automated re-review.
- `references/readiness.md` — final readiness and merge/post-merge boundaries.
