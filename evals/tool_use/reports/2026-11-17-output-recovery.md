# Recoverable codemode text output: capability pilot

All six runs retrieved the correct row outside the script preview without regenerating
the report. Full scenario results were Claude 3/3 and GPT 0/3: GPT added `overwrite: true`
to the final write, which the exact scripted approval did not authorize. One GPT run also
mistyped the output ID, received `not_found`, and corrected it. These failures remain in
the recorded results; no attempts were silently retried or excluded.

## Scope and acceptance

This is an instructed capability probe, not an adoption or efficiency comparison. The
first prompt explicitly requests one codemode report of 6,000 rows; the follow-up requests
row 4500 through `read_output` and an exactly approved file write. The objective verifier
checks the row against the generating formula. Event inspection additionally confirms one
producer script per attempt and a successful artifact read containing the expected row.
The automated scenario gate requires no tool errors and the exact write arguments.

There is no codemode-off or previous-codemode live control: that forced-script task is
unsuitable for comparing those configurations. No general reliability, efficiency, or
default-setting claim follows from these six runs. Failure-output recovery, character
paging, storage failures, quotas, cancellation, and cleanup are tested deterministically,
not covered by this live task.

## Setup and results

- Models: `openai-codex::gpt-5.6-luna` and `anthropic::claude-sonnet-5-5`, effort `medium`;
  three repetitions each. No Gemini calls.
- Source: `2e11d3e874122ae5aaa1cc6336cdc0de4ade86ca` plus local artifact changes.
  The pilot ran while footer placement and character-paging edge cases were being finalized;
  it is not an immutable final-source benchmark. Rows retain tool/instruction/task hashes.
- Task: `codemode-output-recovery`, default script/output budgets, 240-second scenario
  deadline, 12 tool iterations per prompt. Two models in flight; no backend restart.
- Six of six attempted the correct final answer. Claude's three exact writes were approved.
  GPT's three writes were denied for the additional argument; one GPT attempt needed two
  reads to correct its copied ID. All six made exactly one producing script call.
- Estimated priced API usage: **$0.2024912**. GPT subscription usage is unpriced.
  Provider-normalized tokens including cache usage: **485,855** across both providers.
  Configured ceilings: $3 priced usage and 500,000 subscription tokens, checked between rounds.
- Sanitized rows: `../baselines/2026-11-17-output-recovery.json`. Synthetic raw traces remain
  in ignored `results/2026-11-17-output-recovery/`.

Command:

```bash
UV_CACHE_DIR=/tmp/wisp-uv-cache uv run --no-sync python -m evals.tool_use run \
  --models openai-codex::gpt-5.6-luna,anthropic::claude-sonnet-5-5 \
  --tasks codemode-output-recovery --codemode python --effort medium \
  --reps 3 --workers 2 --budget-usd 3 --budget-tokens 500000 \
  --results evals/tool_use/results/2026-11-17-output-recovery
```

## Capability limits

Artifacts reuse the process-owned output store and its eviction/cleanup; they do not survive
backend restart. The run must offer the actual `read_output` tool to enable automatic saving.
The model-facing preview remains bounded by `ToolContext`; combined printed/final text has
a 1 MiB default hard cap. Output beyond that cap is unrecoverable. Success artifacts preserve
existing result-text composition, including trailing-print-newline normalization; failure
artifacts preserve retained printed text. Images and durable session artifacts remain separate
follow-ups. Existing read-only execution policy and codemode availability defaults are unchanged.

## Local verification

- Focused output/state/session/evaluation/docs tests: 319 passed.
- `uv run --no-sync pytest tests/tools tests/coding tests/evals tests/repository -q`:
  2,127 passed, five skipped, six process-inspection/cleanup failures inside the sandbox.
  All six failing cases passed outside the sandbox, without code changes.
- `uv run --no-sync ruff format --check .`, `uv run --no-sync ruff check .`, and configured
  `uv run --no-sync mypy`: passed (267 source files for mypy).
- `uv run --no-sync python -m wisp.rpc.protocol_schema --check`, `git diff --check`:
  passed. No wire/event schema changes.
- `cd site && npm run build`: passed including output links and anchors. Browser layout
  tests were not rerun for this documentation-text change.
- Scoped complexity and duplication reviews completed. Changed parser/preview branches were
  reviewed; duplication findings were outside changed code. Coverage tooling was unavailable;
  no tools were installed and mutation testing was skipped.
- Independent read-only review of storage, paging, state rollback, caps and cancellation:
  no actionable findings. Full repository tests and GitHub CI were not run for this local slice.
