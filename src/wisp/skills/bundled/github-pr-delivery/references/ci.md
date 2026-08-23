# Current-Head CI

Before each monitor starts, record the PR, head SHA, push time, and expected required-check identities.
Obtain the expected set from repository policy, repository instructions, or a previously verified
complete run; do not infer completeness from the checks that happen to be visible immediately after
the push. Query results for that head and wait until every expected check is present and terminal.

Treat queued, pending, cancelled, timed-out, action-required, stale-head, and unexplained skipped
required checks as non-passing. Never infer green CI from an older SHA.

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
gh pr checks "$PR" --required --json bucket,name,state,link,workflow --jq '
  {
    passed: ([.[] | select(.bucket == "pass")] | length),
    observed: ([.[] | {workflow, name, bucket}] | sort_by(.workflow, .name)),
    pending: ([.[] | select(.bucket == "pending") | {workflow, name}] |
      sort_by(.workflow, .name)),
    skipped: ([.[] | select(.bucket == "skipping") | {workflow, name, state, link}] |
      sort_by(.workflow, .name)),
    blocking: ([.[] | select(.bucket != "pass" and .bucket != "pending" and
      .bucket != "skipping") |
      {workflow, name, bucket, state, link}] | sort_by(.workflow, .name))
  }'
```

Use the stable workflow/name pair as the check identity, and preserve the expected identity set outside
the changing post-push snapshot. Compute missing expected identities on every observation and classify
them as pending. If the required set cannot be established, report that limitation instead of treating
an empty or partial visible set as complete. If the available interface cannot filter required checks
reliably, query all checks internally but restrict readiness classification to the recorded expected
identities. Optional check state may be reported separately as a caveat, but it must not block readiness
or trigger a repair unless repository policy or the user puts it in scope.

Before starting, record the expected head with a minimal `headRefOid` query. When managed Bash or an
equivalent resumable process is available, run the snapshot at the normal 15–30 second interval inside
one monitor. Keep the complete normalized observation internal, compare it with the previous object,
and emit a compact summary containing counts plus pending, missing, skipped, or blocking identities
only for:

- the first observation;
- a changed pending, skipped, or blocking set;
- the terminal classification; or
- a query, parsing, authentication, or rate-limit error.

Do not echo unchanged snapshots from the monitor. `gh pr checks` may return status 8 for valid pending
checks or status 1 for a valid failing result, so validate and classify its structured output instead
of treating every nonzero status as a transport failure. Conversely, never turn missing or malformed
JSON into an empty passing set.

Re-read `headRefOid` whenever the monitor emits a changed or terminal state and immediately before a
success claim. If it differs from the expected SHA, stop the monitor, record the replacement SHA, and
restart observation for that head. Success requires every expected identity to be present and in an
accepted terminal state, with no pending or blocking check in that expected set. Passing is accepted;
a skipped required check is accepted only after repository policy or check-specific evidence explains
why that identity is intentionally inapplicable for this head. Record that classification. Failed,
cancelled, and unexplained skipped required checks remain non-passing.

After a blocking result, inspect only the identified check using its recorded provider and link. For a
GitHub Actions check, preserve or derive the selector from that link, verify the selected run's
`headSha` matches the expected PR head, then use `gh run view RUN_ID --log-failed` or
`gh run view --job JOB_ID --log-failed`. Never use selector-free `gh run view --log-failed`, which can
open an interactive chooser or select an unrelated run. For external CI, use its structured connector,
API, or recorded external link instead of `gh run view`. Report inaccessible evidence as a blocker only
after the provider-appropriate path is unavailable. Retrieve broader logs only if focused evidence
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
