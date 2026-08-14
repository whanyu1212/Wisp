# Final Readiness

Refresh state immediately before the final report. Verify:

- the PR is open and, unless intentionally draft, ready for review;
- local task HEAD equals the pushed branch head;
- all required checks for that exact head are terminal and successful;
- any required clean review applies to the current head;
- no unresolved, non-outdated actionable thread remains;
- no newer comment or review invalidates the verdict;
- merge conflict and policy-block status are known;
- no task-owned change remains uncommitted;
- unrelated work remains untouched and is reported separately when relevant;
- the title/body describe the final net diff.

Report PR URL, head SHA, CI result, review result, actionable thread state, mergeability, and caveats.
Keep these claims separate: **CI green**, **review clean**, and **mergeable** are not synonyms.

Merge only with explicit authorization. After a requested merge, verify the remote merged state before
performing any separately authorized update, branch deletion, release, deployment, or cleanup. Never
infer permission for those post-merge effects from permission to merge.
