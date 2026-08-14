# Current-Head CI

After every push, record the PR, head SHA, push time, and required checks. Query check results for that
head and wait until every required check reaches a terminal conclusion.

Treat queued, pending, cancelled, timed-out, action-required, stale-head, and unexplained skipped checks
as non-passing. Never infer green CI from an older SHA.

For each failure:

1. inspect the job and actionable logs;
2. classify it as branch-owned, flaky, external/infrastructure, or independent/pre-existing;
3. when fixes are authorized, make the narrowest branch-owned correction and add regression coverage
   when useful;
4. rerun relevant local checks, commit, push, record the replacement SHA, and restart CI observation;
5. report inaccessible or independent failures as blockers/caveats rather than changing unrelated code.

Poll at bounded intervals appropriate to the repository, typically 15–30 seconds, and provide concise
progress updates during long waits. For one plausibly flaky failure, inspect evidence before requesting
a no-code rerun. Permit one rerun when the same head previously passed or logs support an external or
timing cause. If it repeats, diagnose it as real or report an external blocker; do not rerun forever.


## Clean-delivery terminal condition

When the authorized goal is clean or merge-ready delivery, a terminal failure is a diagnosis point,
not a handoff point. Inspect it and then fix branch-owned behavior, perform the single justified flaky
rerun described above, or establish an external blocker. Continue polling after each action until all
required checks on the current SHA are terminal and successful. Only a genuine blocker—such as
inaccessible logs, persistent unrelated infrastructure failure, or missing authorization for a needed
code change—ends the loop without green CI.

Green CI does not make an earlier review current. After the final green result, proceed to a fresh
exact-head review whenever clean review is part of the delivery contract.
