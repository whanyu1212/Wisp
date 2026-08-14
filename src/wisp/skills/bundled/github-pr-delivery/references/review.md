# Thread-Aware Review

Inspect all relevant surfaces: issue comments, formal reviews and decisions, inline threads, thread
resolution/outdated state, file/line context, timestamps, and reviewed commit identifiers. A flat list
of comments is not sufficient thread state when the platform exposes richer data.

For each unresolved, non-outdated actionable finding:

1. refresh the thread and current head;
2. verify the diagnosis against current code rather than accepting it mechanically;
3. apply a narrow fix and strengthen regression coverage when appropriate;
4. run focused and required project checks;
5. commit and push before saying the finding is fixed;
6. reply with the fixing commit, rationale, and verification;
7. resolve only the thread actually addressed; explain invalid findings with evidence;
8. return to current-head CI after every push.

Resolved or outdated threads are historical context, not active findings.

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
7. require both a current-head clean result and zero unresolved non-outdated actionable threads;
8. if findings require a push, wait for replacement CI and trigger a new review for the new head;
9. keep polling until that exact-head review is clean and all actionable threads are resolved, or a
   genuine external blocker is established.

Use bounded polling, commonly around 30 seconds. Follow repository precedent for stall timing; absent
one, about ten minutes is a reasonable point to post one fresh independently tracked trigger. Silence,
a vanished reaction, an empty transient result, or a stale clean review is not success.


Never treat a clean verdict on a superseded commit as sufficient. Every pushed fix, including a
test-only fix prompted by review, requires a new exact-head trigger after replacement CI is green.
Do not conclude a merge-ready delivery while the latest trigger is absent, queued, or awaiting a
verdict.
