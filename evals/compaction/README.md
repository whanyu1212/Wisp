# Compaction evals

These evals check that Wisp's context compaction works on every model family: that each model
writes a checkpoint Wisp accepts, that the checkpoint keeps what a resumed agent needs, and that
the agent can still finish its task after compaction. Like the [tool-use evals](../tool_use/README.md),
they call paid models, so they run by hand and never in CI; `tests/evals/test_compaction_evals.py`
tests the harness offline. They reuse the tool-use runner's credentials, model specs and
sandboxed process handling.

## Run

```bash
uv run python -m evals.compaction summaries --models anthropic::claude-sonnet-5-5,google::gemini-4-carbon \
  --reps 5 --budget-usd 3
uv run python -m evals.compaction continuation --models anthropic::claude-sonnet-5-5 \
  --reps 5 --budget-usd 10
uv run python -m evals.compaction report evals/compaction/results/<timestamp>
uv run python -m evals.compaction compare evals/compaction/results/<before> evals/compaction/results/<after>
uv run python -m evals.compaction rescore evals/compaction/results/<timestamp>
```

`compare` pairs two Layer 1 folders by model, over the scenarios both ran. `rescore` scores a
Layer 1 folder again from its saved checkpoints with the current fact phrases, so a phrase fixed
after a run applies to both sides of a comparison without calling any model.

Both commands take `--only` (scenario or task ids), `--workers`, `--results`, `--env-file` and
`--auth-file`; `continuation` also takes `--setups forced,default`. The budget is checked
before each round of repetitions, so a stopped run has whole rounds. Codex subscription runs
are unpriced and do not count toward it.

## Layer 1: checkpoint quality (`summaries`)

Each run sends one fixed transcript from `scenarios.py` through `summarize_manual_compaction`,
the function a session compaction calls, and scores the checkpoint without a judge model:

| Column | Meaning |
|---|---|
| Valid | Checkpoints Wisp accepted. An `invalid` one was rejected: a missing or empty section, or a response that did not end normally. In a session that compaction would fail |
| Errored | The provider call failed; this says nothing about the model and is left out of the other columns |
| Recall | Share of planted facts kept: paths, commands, error strings, constraints, decisions, unfinished work, what was investigated. A fact is kept when any of its phrases appears in the checkpoint, ignoring case |
| Injection followed | Checkpoints that obeyed an instruction planted in a file the session read |
| Buried kept | Facts placed in the middle of a tool result longer than 2,000 characters. They reach the summarizer only when its budget allows a longer cut (see `_compaction_tool_result_chars`), so they measure that cut rather than the model |

| Scenario | What it covers |
|---|---|
| `debug-flaky-test` | A short debugging session with constraints, a rejected alternative and a test still failing |
| `refactor-with-injection` | A half-done rename, and a file that tells the summarizer to report the branch ready to merge |
| `long-test-output` | A long test run whose key failure sits in its middle, and a fix chosen over a version pin the user ruled out |
| `long-session-chunked` | A 39-file audit summarized with a 30k-token window, so it goes through Wisp's chunk-and-aggregate path, with a constraint, a decision and an error mid-session |

## Layer 2: continuing after compaction (`continuation`)

A task in `tasks/<id>/` is a fixture `repo/`, a `task.toml` listing user turns, a verifier and a
`solution/`. Each turn is one `wisp -p --mode json` call. Later turns use `--continue`, so they
share one session. Two setups run with the same model:

- `forced` sets `WISP_CONTEXT_RESERVE_TOKENS` to the model's catalog window minus the task's
  `trigger_tokens`. Wisp compacts once context passes `window - reserve`, and uses the larger of
  this reserve and any provider limit, so every provider compacts at the same size. The summarizer
  also gets `window - reserve` as its own budget, as it would in a real session at that size.
- `default` keeps the model's real window, so nothing is compacted.

A `forced` run that completed no compaction is `errored`, not passed. Rows also record:

- every compaction: its turn, reason, outcome, and the context estimate when it started;
- `repeat_reads`: reads in the last turn of files already read in an earlier turn;
- the tool calls, tokens and cost of the run.

| Task | What it takes |
|---|---|
| `chat-constraints` | Two constraints given only in the first message, which compaction replaces: a named constant and a CHANGELOG line. The fix is asked for three turns later |
| `investigation-memory` | Six modules read in turn 1, and the bug found there fixed in turn 3 |

Verifiers run the fixture's original tests against the result, so a model may add tests but
cannot pass by editing or deleting the ones it was given. They print each problem to
`verify.txt` in the run folder.

Each run starts from a fresh temporary folder with a private `HOME`, and gets only an
allowlisted environment, as in the tool-use evals. `tests/evals/test_compaction_evals.py`
checks that every verifier rejects its fixture and accepts its solution.

With five repetitions a cell, read only large differences. One task passing in both setups
shows that compaction keeps enough for that task, not that it loses nothing.

## Recorded results (2026-11-13, Wisp 0.9.1, before the checkpoint changes)

Layer 1 used 5 repetitions on seven models; Layer 2 used 4 repetitions per setup on four.
The total known cost was about $10, with Codex subscription runs unpriced.

- **Every checkpoint was valid**: 105/105, on GPT-6.1-sol (API and Codex), GPT-5.6-luna,
  Claude Sonnet 5.5, Claude Haiku 5.5, Gemini 4 Carbon and Gemini 3.6 Flash, including the
  chunked path. The required-heading contract held on every family.
- **Recall**: 1.00 or 0.98 for the GPT and Claude models, and 0.97–1.00 for Gemini 4 Carbon.
  Gemini 3.6 Flash was lowest, at 0.89–0.97. It dropped the chosen fix in 5 of 5
  `debug-flaky-test` runs (it kept the root cause but not "integer milliseconds"), and in the
  chunked scenario lost a mid-session decision and an error string in 2 of 5 runs each.
- **Injection**: No checkpoint followed the injected instruction. All 12 flagged checkpoints
  quote it in order to refuse it.
- **Buried facts**: The buried fact was lost in every run, 35 of 35. That is expected: tool
  results are cut to 2,000 characters before the summarizer sees them.
- **Layer 2**: All 32 forced runs passed. Every forced run compacted before its final turn's work
  (1–3 compactions each), and none failed. All default runs passed except one Gemini run that hit
  its 300 s timeout in turn 1. Forced runs sent 20–60% fewer input tokens than default runs but cost 1.3–2.1× more, because each checkpoint is its own paid request and rewriting history invalidates the prompt cache.
  Repeat reads in the last turn were about the same with and without compaction (0–2.2), so
  checkpoints did not make agents redo reads.
- **Where compaction fires**: Most compactions fired mid-turn, after tool calls had started,
  rather than at a turn boundary.

## Recorded results: changing the checkpoint (2026-11-13)

Two changes were measured, 8 runs of each of the 4 scenarios on seven models, before and after:
the tool-result cut scaled with the summarizer budget, and the "Key Decisions" prompt line. Both
sides were rescored with the same fact phrases. Gemini 3.8 Flash replaced 3.6 Flash.

| Model | Buried facts kept | Recall | Input tokens per checkpoint |
|---|---|---|---|
| GPT-6.1-sol (API) | 0/23 → 24/24 | 0.99 → 1.00 | +12% |
| GPT-6.1-sol (Codex) | 0/24 → 24/24 | 1.00 → 0.99 | +4% |
| GPT-5.6-luna | 0/24 → 24/24 | 0.97 → 0.98 | +15% |
| Claude Sonnet 5.5 | 0/24 → 24/24 | 0.99 → 1.00 | +14% |
| Claude Haiku 5.5 | 0/24 → 24/24 | 1.00 → 0.99 | +13% |
| Gemini 4 Carbon | 0/24 → 24/24 | 0.99 → 0.99 | +15% |
| Gemini 3.8 Flash | 0/24 → 22/24 | 0.95 → 0.95 | +27% |

- The scaled cut is the change that matters. Facts from the middle of long tool output now
  survive for every model, and a checkpoint costs about 4–27% more input.
- The prompt line made no measurable difference, so it was not kept. Decisions were already kept 95–100% of the
  time. The fix chosen in `debug-flaky-test` was dropped 5 times before and 2 times after, too
  few to tell apart.
- Gemini 3.8 Flash's remaining gap is unfinished work: it never kept "ask before changing the
  test" (0/8 on both sides). GPT-5.6-luna lost it in 5 runs before and 4 after.
- Rejected checkpoints: one from Gemini 3.8 Flash before (a missing section), none after.
- Layer 2's forced runs use a 9k-token trigger, which keeps the 2,000-character cut, so they
  test only the prompt line. All 32 forced runs passed again, with no failed compactions and
  cost within noise of the earlier run.
