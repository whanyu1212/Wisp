# Codemode presentation: bounded live pilot

**Recommendation:** retain `direct_and_codemode` as the default. This pilot does not satisfy the planned all-verifiers-pass rollout gate. Correct the evaluator issues below and rerun the affected scenarios before calling live validation complete. No runtime source or default was changed during evaluation.

## Design and accounting

- Source: `7e9382808b0985dca94d8ad38ed36ff7a4be16e9` (PR #1060).
- Models: `openai-codex::gpt-6.1-sol` and `openai-codex::gpt-5.6-luna`, both medium effort. Existing subscription authentication; no API dollar price is available for these runs. Reported priced usage is $0, not a claim of zero economic cost.
- Three arms on the same source: codemode off; Python codemode with direct tools; Python codemode only. MCP exposure `on_demand`, results `structured`. Task, fixture and catalog hashes match across arms; offered-schema hashes are stable within each task/model/arm. Hashes are saved in the baselines.
- Tasks: local failing-test repair; 34-PR MCP lint fan-out; session state through manual compaction and backend restart/resume with a write approval.
- Three repetitions planned, rotating arm order: off/direct/only; only/off/direct; direct/only/off. Two workers, task caps of 300/600/300 seconds respectively.
- Completed **48/54** attempts: off 12, direct 18, only 18. The shared two-million-token ceiling was checked between six-run batches; the final batch brought usage to **2,093,439** normalized input+output tokens. The final six off runs were skipped. No failures were silently rerun.
- Main comparison uses only common repetitions 1–2, twelve attempts per arm. Third repetitions are retained separately; this small synthetic pilot does not establish statistical equivalence or general production reliability.

## Matched results

Statuses below remove only the known obsolete schema setup check for codemode-only. Approval failures remain failures. Costs include every attempt, including failures; seconds include setup, execution and verification.

| Arm | Passed | Failed | Mean tokens/attempt | Mean seconds/attempt |
|---|---:|---:|---:|---:|
| off | 9/12 | 3 | 35,809 | 47.9 |
| direct | 10/12 | 2 | 35,127 | 31.1 |
| only | 9/12 | 3 | 60,513 | 45.3 |

Each cell below is **passes/2; mean tokens; mean seconds**. These are descriptive per-pair means, not significance claims.

| Task | Model | Off | Direct + codemode | Codemode only (audited) |
|---|---|---|---|---|
| fix-failing-test | gpt-5.6-luna | 2/2; 24,282; 20.3s | 2/2; 29,454; 21.3s | 2/2; 46,473; 33.2s |
| fix-failing-test | gpt-6.1-sol | 2/2; 28,082; 29.3s | 2/2; 30,188; 22.6s | 2/2; 44,060; 37.9s |
| mcp-failing-lint | gpt-5.6-luna | 0/2; 42,682; 69.5s | 2/2; 35,648; 21.4s | 2/2; 110,087; 55.6s |
| mcp-failing-lint | gpt-6.1-sol | 2/2; 41,868; 63.1s | 2/2; 42,618; 31.2s | 2/2; 71,284; 40.5s |
| mcp-session-reuse | gpt-5.6-luna | 1/2; 43,336; 48.4s | 2/2; 37,488; 40.3s | 1/2; 54,381; 55.3s |
| mcp-session-reuse | gpt-6.1-sol | 2/2; 34,604; 56.9s | 0/2; 35,369; 49.6s | 0/2; 36,792; 49.1s |

## Raw verdicts and evaluator limitations

- Off raw: **9 passed, 3 failed** (12 attempts). Direct raw: **14 passed, 4 failed** (18 attempts). Only raw: **6 passed, 12 errored** (18 attempts). Only audited: **14 passed, 4 failed**. Across all three repetitions the two candidate arms each passed all six coding and all six fan-out attempts, and two of six strict session scenarios. These unequal sample totals must not be pooled into a balanced three-arm comparison.
- `runner.py::_mcp_setup_problem` requires legacy `mcp_search`, `mcp_describe`, and `mcp_call` for on-demand MCP even under codemode-only. The intended offered surface is `tool_search`, `tool_describe`, and `codemode`. It sets a runner error although execution and the objective verifier still run. All twelve only-arm MCP rows match this exact defect. The separate audited baseline removes this check only after checking the exact offered surface, no ErrorEvent metrics, and successful nested MCP execution, then reuses the existing classifier. Raw verdicts and metrics remain available unchanged.
- `scenario_driver.py` matches approval arguments by exact JSON equality. The session fixture expects only `path` and `content`; eight candidate attempts requested the correct output with an additional `overwrite` field (six false, two true). The runtime default is true; false is create-only. The fixture denied these writes and the task/scenario verifiers failed. They remain failures, including in the audited baseline; a correct proposed answer is not an executed successful write.
- The three off-arm failures are Luna answer errors: both fan-out attempts and session repetition 1. In fan-out repetition 1 the correct 34 MCP responses were available but the final answer substituted two PR IDs; session repetition 1 omitted PR 528. No approval-shape correction applies to these failures.

## Trace evidence and limits

- With direct tools available, both models chose direct tools for local coding and scripts for MCP fan-out. Codemode-only required additional discovery and script work; mean tokens were higher in all six matched task/model pairings. Fan-out was faster with direct-plus-codemode in this sample. Fewer outer calls alone are not treated as reduced work: MCP fan-out still requires the underlying target calls.
- The independently inspected first two candidate session repetitions show stored data loaded after restart and exact final answer content, with no post-resume MCP refetch. **Three of those eight candidate scenarios passed end to end** (direct/Luna twice and only/Luna once); the remaining five were denied at approval. All twelve candidate session attempts have correct proposed write content and no post-resume MCP calls in the event audit. Off/Luna repetition 2 refetched once; off-arm passes do not prove codemode state reuse.
- Some scripts failed and recovered; all script failures and nested-call metrics are preserved. Script-text `store(`/`load(` counts are attempts, not successful operations. Successful state reuse claims above rely on event outputs, not lexical counts. Nested work on failed scripts must include `failed_script_calls`, not just `nested_calls`.
- Objective verifiers establish final test/output correctness and scenario steps; they do not independently prove discovery mechanism, state reuse, or absence of refetch. Raw trace inspection supplies those narrower observations. This pilot does not replace adversarial deterministic coverage for permissions, cancellation, isolation or duplicate mutations, and does not cover image output or durable artifacts.

## Artifacts and next gate

Sanitized baseline files: [off](../baselines/2026-11-17-presentation-off.json), [direct](../baselines/2026-11-17-presentation-direct.json), [only raw](../baselines/2026-11-17-presentation-only-raw.json), and [only audited](../baselines/2026-11-17-presentation-only-audited.json). The audited file links each original event artifact by SHA-256 and records its original verdict. Raw traces, frozen plan, launcher, audit script and derived rows remain locally in ignored `results/2026-11-17-presentation-pilot/`.

Before a clean live gate: make the setup assertion presentation-aware, define approval fixture matching that accepts the intended safe optional argument semantics while still rejecting wrong paths/content, add focused regressions, and rerun the affected session scenarios with a fresh bounded budget. Do not relabel denied writes as passes or change the default based on this pilot. No application fixes, new push, merge, or release were performed as part of this evaluation.
