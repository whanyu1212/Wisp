# Thread-Aware Review

Inspect all relevant surfaces: issue comments, formal reviews and decisions, inline threads, thread
resolution/outdated state, file/line context, timestamps, and reviewed commit identifiers. A flat list
of comments is not sufficient thread state when the platform exposes richer data.

For each unresolved actionable finding:

1. refresh the thread and current head;
2. verify the diagnosis against current code rather than accepting it mechanically;
3. apply a narrow fix and strengthen regression coverage when appropriate;
4. run focused and required project checks;
5. commit and push before saying the finding is fixed;
6. reply with the fixing commit, rationale, and verification;
7. resolve only the thread actually addressed; explain invalid findings with evidence;
8. return to current-head CI after every push.

Resolved threads are historical context. An unresolved outdated thread is not automatically cleared: revalidate its finding against the current head, then fix or rebut it with current evidence before excluding it from the actionable set.

## Exact-head automated re-review

Run this loop only when the user requests automated review/iteration-until-clean or repository policy
requires it:

1. wait for current-head CI unless concurrent review is explicitly useful;
2. record the current head SHA;
3. post a fresh repository-appropriate review trigger and record its ID/time;
4. treat acknowledgement reactions as queued signals, not verdicts;
5. inspect reviews, comments, reactions, and threads created after the trigger;
6. accept clean only when the verdict identifies the recorded commit, or arrives after the trigger and
   the PR head has not changed;
7. require both a current-head clean result and zero unresolved actionable threads; revalidate outdated unresolved findings before classifying them as non-actionable;
8. if findings require a push, wait for replacement CI and trigger a new review for the new head;
9. keep polling until that exact-head review is clean and all actionable threads are resolved, or a
   genuine external blocker is established.

Use bounded polling, commonly around 30 seconds. Follow repository precedent for stall timing; absent
one, about ten minutes is a reasonable point to post one fresh independently tracked trigger. Silence,
a vanished reaction, an empty transient result, or a stale clean review is not success.

### Compact review monitor

Use one managed monitor when available rather than issuing a visible full review query on every
interval. Its normalized state must contain the current head SHA, the tracked trigger ID or timestamp,
the sorted IDs and creation/edit versions of issue comments added after the trigger, every formal
review submitted after the trigger, any candidate verdict state, and the sorted IDs and
resolution/outdated state of every unresolved thread. Represent a comment version with `updatedAt` or
`lastEditedAt` when available, or a stable body hash; reclassify an observed comment whenever that
version changes. Snapshot review IDs and states at trigger time, then detect later submission timestamps
and state transitions even when a pending review object was created earlier.
Do not restrict the post-trigger inventory to the expected automation account: another reviewer may
add actionable feedback while automated review is running. For GraphQL, paginate every relevant
connection but initially project only IDs, comment creation/edit version, review `submittedAt`, state
and commit identity, and thread `isResolved`/`isOutdated` state. Paginate each thread's comment
connection and retain the complete sorted set of comment ID/version pairs already observed; on later
polls, surface every unseen or changed pair, or fetch the complete delta after the last recorded cursor.
Never reduce a multi-comment delta to only the newest reply.

Emit the initial state and then only changes: a new acknowledgement, issue comment, formal review, or
edited comment, verdict, changed thread set, clean exact-head terminal result, stale head, or explicit
query error. Do not repeatedly return full review bodies, file context, reactions, or complete comment
histories. When any post-trigger item, new comment version, or unresolved thread first appears, leave
the monitor and fetch that item's body, author, path/line context, reactions, submission state, and
commit association once for that version's classification. Record the classification and inspect every
unseen or changed inline comment and every newly submitted or state-changed review before accepting a
clean verdict. Resume compact monitoring after a finding is handled and any replacement head has green
CI.

An empty or malformed response, incomplete pagination, authentication failure, rate limit, or monitor
exit without a classified terminal state is an error, not a clean review. If managed execution is not
available, use the same minimal projections as bounded snapshots and preserve the exact-head and
thread-aware criteria above.


Never treat a clean verdict on a superseded commit as sufficient. Every pushed fix, including a
test-only fix prompted by review, requires a new exact-head trigger after replacement CI is green.
Do not conclude a merge-ready delivery while the latest trigger is absent, queued, or awaiting a
verdict.
