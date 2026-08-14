# Preflight and Packaging

## Inspect before writing

1. Re-read the request and repository instructions.
2. Inspect the current branch, upstream, remotes, worktrees, status, untracked files, diff, diff stat,
   and recent commits.
3. Inspect relevant implementation, callers, tests, and recent history.
4. Define the exact task-owned file list. Treat all other modifications as user-owned.
5. Fetch the target remote before deciding whether the base or branch is current. Verify ancestry with
   `git merge-base --is-ancestor`; object-ID inequality alone does not prove divergence.
6. Confirm GitHub authentication before remote writes.

Never discard, overwrite, stage, reformat, or relocate unrelated changes to obtain a clean tree. If the
current worktree contains concurrent work, use a separate worktree and focused branch when practical.
Do not remove another process's worktree or lock.

## Verify the change

Start with focused tests, then run repository-required format, lint, type, and broader test gates when
proportionate. Record exact commands and terminal outcomes. A timeout, pending command, inaccessible
service, or cancelled check is not a pass. A disclosed local limitation may still justify opening a PR
for remote evidence if the user requested delivery.

## Package narrowly

1. Stage explicit task paths. Avoid whole-worktree staging unless every change is verified in scope.
2. Inspect staged names, diff, stat, and `git diff --cached --check`.
3. Scan staged content for credentials, private data, generated debris, debug logs, and unrelated edits.
4. Follow repository commit conventions and preserve the configured author identity. Apply any
   instruction-mandated trailer exactly once; otherwise add no generated-by branding.
5. Verify the resulting commit and post-commit worktree state.
6. Push without force. Do not amend, rebase, squash, or otherwise rewrite shared history unless the
   user explicitly authorizes it and repository policy permits it.

## Create or refresh the PR

Describe the final net diff against the base rather than the chronology of attempts. Include:

- delivered outcome and behavior grouped by area;
- compatibility, safety, migration, or persistence properties;
- exact local checks and their outcomes;
- known limitations and intentional follow-ups.

Default to a ready-for-review PR. Use a draft only when explicitly requested or required by repository
policy. After later behavioral pushes, update the title/body if they no longer describe the net diff.
