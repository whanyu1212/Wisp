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

## Compact CI monitor

When `gh` is the available interface, query structured check state instead of starting its table watch.
A compact snapshot can use the following projection (adapt the PR selector and repository flag, but
keep the selected fields minimal):

```bash
gh pr checks "$PR" --json bucket,name,state,link --jq '
  {
    passed: ([.[] | select(.bucket == "pass")] | length),
    pending: ([.[] | select(.bucket == "pending") | .name] | sort),
    blocking: ([.[] | select(.bucket != "pass" and .bucket != "pending") |
      {name, bucket, state, link}] | sort_by(.name))
  }'
```

Before starting, record the expected head with a minimal `headRefOid` query. When managed Bash or an
equivalent resumable process is available, run the snapshot at the normal 15–30 second interval inside
one monitor. Serialize the normalized object deterministically, compare it with the previous object,
and emit only:

- the first observation;
- a changed pending or blocking set;
- the terminal classification; or
- a query, parsing, authentication, or rate-limit error.

Do not echo unchanged snapshots from the monitor. `gh pr checks` may return status 8 for valid pending
checks or status 1 for a valid failing result, so validate and classify its structured output instead
of treating every nonzero status as a transport failure. Conversely, never turn missing or malformed
JSON into an empty passing set.

Re-read `headRefOid` whenever the monitor emits a changed or terminal state and immediately before a
success claim. If it differs from the expected SHA, stop the monitor, record the replacement SHA, and
restart observation for that head. Success requires no pending checks and no blocking buckets; failed,
cancelled, and unexplained skipped checks remain non-passing. After a blocking result, inspect only the
identified run or job with `gh run view --log-failed`; retrieve broader logs only if that focused output
cannot explain the failure.

If resumable execution is unavailable, repeat the same projection as short snapshots and keep user
progress updates change-based. Do not fall back to a redraw-heavy watch command merely to avoid a
managed process.


## Clean-delivery terminal condition

When the authorized goal is clean or merge-ready delivery, a terminal failure is a diagnosis point,
not a handoff point. Inspect it and then fix branch-owned behavior, perform the single justified flaky
rerun described above, or establish an external blocker. Continue polling after each action until all
required checks on the current SHA are terminal and successful. Only a genuine blocker—such as
inaccessible logs, persistent unrelated infrastructure failure, or missing authorization for a needed
code change—ends the loop without green CI.

Green CI does not make an earlier review current. After the final green result, proceed to a fresh
exact-head review whenever clean review is part of the delivery contract.
