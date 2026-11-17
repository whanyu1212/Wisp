# Live rerun after evaluator fixes

This is fresh evidence using the corrected evaluator, not a rescoring of the earlier pilot. All original statuses are retained. The local coding task was not rerun; this run targets the affected MCP fan-out and session-reuse scenarios.

## Frozen setup

- Base commit: `7e9382808b0985dca94d8ad38ed36ff7a4be16e9`, **with uncommitted evaluator fixes**. Commit identity alone does not identify the tested source.
- Evaluator patch SHA-256: `6b5da4a12f7d6b54e9132b83041585b33435b6a334fa73ba738b865292605bba`.
- Source-manifest SHA-256: `6d3d6e2f151d6c6c79b668b213fb3db7a62c0eacfb2f8167169dc9a48b2adef4`. All recorded source hashes were rechecked after completion. The patch and manifest are retained beside the raw results.
- Models: `openai-codex::gpt-6.1-sol` and `openai-codex::gpt-5.6-luna`, medium effort. Same fixtures/catalogs within this rerun; on-demand MCP with structured results.
- Three arms: off, Python codemode with direct tools, and Python codemode-only. Three repetitions, two workers; order rotated off/direct/only, only/off/direct, direct/only/off.
- Completed **36/36 attempts**, using **1,941,619 normalized input+output tokens**. Two-million-token budget checked between four-run batches, $10 priced-usage ceiling, 25-minute overall hard cap. Subscription usage is unpriced; reported priced usage is $0, not a statement of zero economic cost.
- MCP fan-out task cap: 600 seconds. Session task cap: 300 seconds. No automatic retries or replacement attempts.

## Objective outcomes

| Arm | Passed | Failed | Errored | Mean tokens | Median tokens | Mean seconds |
|---|---:|---:|---:|---:|---:|---:|
| off | 10/12 | 1 | 1 | 65,107 | 36,553 | 59.5 |
| direct | 12/12 | 0 | 0 | 40,797 | 39,880 | 37.6 |
| only | 12/12 | 0 | 0 | 55,898 | 50,403 | 44.0 |

Each cell below is **passes/3; mean tokens; mean seconds**. Cost means include every attempt, including failures. Seconds include setup and verification. These small-sample comparisons are descriptive, not evidence of statistical equivalence.

| Task | Model | Off | Direct + codemode | Codemode only |
|---|---|---|---|---|
| mcp-failing-lint | gpt-5.6-luna | 1/3; 143,274; 77.5s | 3/3; 32,970; 22.8s | 3/3; 67,543; 35.7s |
| mcp-failing-lint | gpt-6.1-sol | 3/3; 38,851; 60.7s | 3/3; 35,093; 26.4s | 3/3; 78,123; 45.9s |
| mcp-session-reuse | gpt-5.6-luna | 3/3; 41,562; 46.2s | 3/3; 55,845; 51.0s | 3/3; 41,959; 46.1s |
| mcp-session-reuse | gpt-6.1-sol | 3/3; 36,742; 53.7s | 3/3; 39,279; 50.1s | 3/3; 35,965; 48.5s |

The two non-passes were both off/Luna fan-out attempts: repetition 1 wrote an incorrect PR set (503, 509, 516, 528); repetition 3 hit a provider transport error after the stream began. The latter is infrastructure failure, not an incorrect model answer. Both are retained; the early transport failure also makes its cost incomparable to a completed attempt.

## What the rerun establishes

- The corrected setup check accepts the intended codemode-only surface without post-hoc status changes. The explicit complete approval alternatives allow the intended write arguments while preserving exact target, path, content, type and scope checks.
- Independent trace review confirms successful stored-data loading after compaction and restart in **11/12 candidate session attempts**: direct 5/6, only 6/6. All twelve writes were approved: six explicit `overwrite=false`, five omitted, and one explicit `true` (only/Luna repetition 2). Every candidate session completed all five scenario steps and passed the exact output verifier.
- Direct/Luna repetition 1 passed the final task but refetched after restart instead of loading its retained handle. Its earlier two script failures were API misuse: calling the text-returning MCP proxy as if it returned a dictionary, then attempting to JSON-decode the text. The named MCP function was documented as returning a dictionary. The persisted session still held its data and handle; no runtime persistence defect was established by that trace.
- Task success does not itself prove state reuse. The baseline records post-restart MCP-call counts; lexical store/load counts alone are not used as proof. Some script failures recovered; raw metrics retain them.
- The off/Luna fan-out repetition 2 passed after 72 check-result calls and 42 turns, consuming 401,461 tokens (375,296 cached input tokens). This outlier is included; pooled averages must not be read as typical efficiency.

## Artifacts and scope

Sanitized unchanged-verdict baselines: [off](../baselines/2026-11-17-evaluator-rerun-off.json), [direct](../baselines/2026-11-17-evaluator-rerun-direct.json), [only](../baselines/2026-11-17-evaluator-rerun-only.json). Raw traces and scripts remain locally in ignored `results/2026-11-17-evaluator-rerun/`. No raw credential material is copied into baselines.

This targeted rerun addresses the evaluator blockers in the [earlier pilot](2026-11-17-presentation.md). It does not establish universal reliability, a benefit from making codemode-only the default, or coverage of images/durable artifacts. Retain direct-plus-codemode as the default. No commit, push, merge, release, or runtime-default change was performed during this rerun.
