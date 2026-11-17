# Live evaluation with codemode prompt clarifications

Tested commit `de380b26d9e7a768a6f9602226d68156381e969c` with the clarified return-value, direct-invocation and retained-state guidance. Source hashes were frozen before the run and rechecked after it. All statuses below are original evaluator verdicts; no post-hoc correction or replacement attempts were used.

## Setup

- Same tasks as the preceding evaluator rerun: `mcp-failing-lint` and `mcp-session-reuse`. Same models: `openai-codex::gpt-6.1-sol` and `openai-codex::gpt-5.6-luna`, medium effort. MCP exposure `on_demand`, results `structured`.
- Three arms (off, direct-plus-codemode, codemode-only), three repetitions planned, two workers. Arm order rotates off/direct/only; only/off/direct; direct/only/off. Task, fixture and catalog hashes match within the run.
- **36/36 attempts**, **1,665,996 normalized input+output tokens**. Shared two-million-token ceiling checked before each four-run batch, $10 priced-usage ceiling, 25-minute overall cap. An in-flight batch may overshoot; individual task caps are 600 seconds for fan-out and 300 seconds for session reuse.
- Subscription usage is unpriced. Reported priced usage is $0, not a claim of zero economic cost. No extra live calls were made for trace analysis.

## Results

| Arm | Passed | Failed | Errored | Scripts | Failed scripts | Mean tokens | Median tokens | Mean seconds |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| off | 11/12 | 1 | 0 | 0 | 0 | 44,273 | 38,530 | 61.4 |
| direct | 12/12 | 0 | 0 | 21 | 0 | 36,813 | 38,601 | 38.7 |
| only | 12/12 | 0 | 0 | 35 | 1 | 57,748 | 42,156 | 44.7 |

Each task/model cell is **passes/attempts; mean tokens; mean seconds**. All attempts remain in cost means; elapsed time includes setup and verification. Small descriptive means do not establish statistical equivalence.

| Task | Model | Off | Direct + codemode | Codemode only |
|---|---|---|---|---|
| mcp-failing-lint | gpt-5.6-luna | 2/3; 51,647; 75.4s | 3/3; 30,712; 21.0s | 3/3; 76,733; 40.1s |
| mcp-failing-lint | gpt-6.1-sol | 3/3; 40,490; 62.9s | 3/3; 37,474; 32.1s | 3/3; 76,437; 45.7s |
| mcp-session-reuse | gpt-5.6-luna | 3/3; 49,925; 54.8s | 3/3; 38,322; 41.1s | 3/3; 41,268; 44.5s |
| mcp-session-reuse | gpt-6.1-sol | 3/3; 35,030; 52.5s | 3/3; 40,742; 60.7s | 3/3; 36,552; 48.5s |

## Comparison limits and trace evidence

The [preceding evaluator rerun](2026-11-17-evaluator-rerun.md) is a historical comparison, not a contemporaneously randomized old/new-prompt trial. Fresh off runs help characterize this run but do not isolate the prompt change. Differences may reflect model variability; do not attribute them causally to the wording. The prior candidate arms each passed 12/12, with three failed scripts in direct mode, two in only mode, and one direct-mode session refetch.

- All **24 candidate attempts passed**. Independent trace review covered all candidate traces and confirmed **12/12 session attempts** loaded the same stored handles successfully after compaction/restart, with **zero refetches** and all twelve write approvals granted. The prior run demonstrated reuse in 11/12 sessions.
- Direct-plus-codemode had **zero failed scripts**; codemode-only had **one**. The latter was Luna fan-out repetition 3: it used `asyncio.gather` without `import asyncio`, failed type checking before any tool call, then added the import and succeeded. The previous run had five failed scripts in total; these counts are descriptive observations, not proof of a prompt effect.
- No candidate script imported a nonexistent `tools` module or exhibited the previous text-as-dictionary/JSON misuse. Three direct-arm scripts legitimately called `mcp_call` and printed its text; proxy use alone is not a failure.
- All candidate fan-out and session verifiers passed. The one control failure was Luna fan-out repetition 2: the answer substituted PR 517 for 516. No infrastructure errors occurred. This control failure remains failed.
- The prompt clarifications passed this targeted live check. Keep direct-plus-codemode as the default; the small run does not warrant another prompt change solely for the recovered missing-asyncio-import mistake.

## Artifacts and scope

Sanitized baseline files: [off](../baselines/2026-11-17-clarification-rerun-off.json), [direct](../baselines/2026-11-17-clarification-rerun-direct.json), [only](../baselines/2026-11-17-clarification-rerun-only.json). Each retains task/source fingerprints and links trace evidence by SHA-256. Raw traces, frozen plan, manifest and scripts remain locally in ignored `results/2026-11-17-clarification-rerun/`.

This targeted run checks the two affected MCP workflows. It does not cover every task, provider, or security boundary, and does not justify changing the default to codemode-only. Direct-plus-codemode remains the default. No implementation, default, commit, push or PR update was made as part of this run.
