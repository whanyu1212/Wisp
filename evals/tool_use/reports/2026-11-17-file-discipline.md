# File-tool discipline: current guidance versus explicit reminders

## Decision

Do not add the candidate reminders to the production prompt based on this experiment. Current
instructions and explicit file-tool reminders both passed 6/6 runs without a tool-invocation error
or nonzero shell exit. All four formatter runs reread the changed file before editing it.

The candidate remains opt-in eval data in `guidance/file-discipline.md`. This measures one prompt
intervention, not all ways to improve tool use. Better UI failure explanations do not themselves
change the instructions or results seen by the model.

## Setup

- Model/provider: `openai-codex::gpt-6.1-sol`, default effort, stored subscription credentials.
- Three opt-in synthetic tasks, two repetitions per condition: discover a moved module, disregard
  historical line offsets in a now-small file, and read again after a formatter changes a file.
- Same tools, fixtures, verifiers, runtime, and task prompts in both conditions. Codemode off.
- Candidate: append the 547-byte committed guidance to the isolated fixture's root `AGENTS.md`
  before its initial commit. The checkout's instructions and user settings are not modified.
- One worker per condition, run concurrently; each condition capped at 250,000 unpriced tokens
  and USD 2 of priced responses, checked between rounds. Neither stopped for budget.
- Source base: `895cee72229ad8cd992376bdc3c8d6a9fff6d323` plus the uncommitted presentation/eval
  changes in this PR. No production agent prompt or tool-execution policy changed between runs.

The sanitized baselines retain task, fixture, tools and guidance hashes, not credentials, local
folders or error text:

- `../baselines/2026-11-17-discipline-current.json`
- `../baselines/2026-11-17-discipline-guided.json`

Commands to reproduce are in the parent README. Compare the committed measurements with:

```bash
uv run python -m evals.tool_use compare \
  evals/tool_use/baselines/2026-11-17-discipline-current.json \
  evals/tool_use/baselines/2026-11-17-discipline-guided.json
```

## Results

| Task | Passed, current → guided | Mean calls | Mean input + output tokens | Tool errors |
|---|---|---|---|---|
| Discover moved module | 2/2 → 2/2 | 10.5 → 12.0 | 23,009 → 26,652 | 0 → 0 |
| Read after formatting | 2/2 → 2/2 | 11.0 → 10.5 | 27,212 → 27,888 | 0 → 0 |
| Current file bounds | 2/2 → 2/2 | 9.0 → 9.5 | 24,013 → 19,484 | 0 → 0 |

Every task verifier passed. Every `tool.result` had either no exit code or exit code zero;
nonzero command exits were inspected separately rather than counted as invocation mistakes.
The formatter event sequences were also inspected: both conditions called `read` on `worker.py`
after the formatter and before `edit`.

No calls/tokens change had a 95% interval excluding zero. With two repetitions, the reported
median interval half-width is ±182% for calls and ±224% for tokens. The point estimates cannot
establish an efficiency improvement or regression. Codex runs were unpriced subscription runs;
API-equivalent estimates in reports are not charges.

## Limits and next evidence

These are short, deliberately small tasks on one model. Baseline behavior was already at the
ceiling; this does not reproduce the long-session mistakes that motivated the work, prove a
zero underlying error rate, or evaluate automatic retry mechanisms. The historical-offset task
can be solved by reading the whole tiny file without consulting the old source map.

Keep the current stale-file, unique-match, protected-path and approval safeguards. To justify a
production change, first add a longer, repeated read/format/edit scenario that elicits actual
baseline errors, then rerun across more repetitions and at least another model. Measure task
completion, classified invocation errors, shell outcomes, calls and tokens separately. Do not
reinterpret expected failing tests as successful commands, or hide earlier failures after a retry.
