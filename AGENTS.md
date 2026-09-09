# Wisp Agent Instructions

These shared instructions apply to the entire repository and are tracked in version control.

## Working style

- Inspect the current worktree, relevant callers, tests, and recent implementation before proposing
  or making a non-trivial change.
- Keep changes narrow, dependency-aware, and easy to review. Do not mix unrelated cleanup into the
  active task.
- Preserve all unrelated tracked and untracked work. In particular, do not modify, remove, or stage
  `assets/media_assets.json` unless the user explicitly puts it in scope.
- Do not create branches, commits, pushes, pull requests, merges, or releases unless the user asks
  for that delivery step.
- State assumptions and distinguish verified behavior from hypotheses. Do not claim broader test or
  runtime coverage than the evidence supports.

## Astra collaboration

- Keep Astra as the primary implementer by default, respecting an explicit user model choice.
  The primary agent owns design decisions, implementation, verification, and final integration.
- Use the configured explorer, researcher, and reviewer roles selectively for bounded, independent
  investigation or review. Keep their work read-only and integrate consequential findings against
  the source and test evidence. Follow the global productivity-subagent instructions for role and
  model selection; independent review should focus on correctness and meaningful readability issues.
- Prefer the smallest complete change that meets the user's intent. Explain meaningful tradeoffs
  in plain language and carry authorized work through verification without repeated confirmation.

## Human readability

- Treat readability for human maintainers as a first-class design goal. Optimize for someone
  encountering the code without the conversation history: they should be able to find the entry
  point, understand the normal flow, and locate the details they need.
- Organize modules and subpackages around coherent responsibilities. Separating configuration from
  execution, or grouping a runner with its supporting modules, is worthwhile when it makes the code
  easier to navigate even if behavior and total line count stay the same.
- Keep orchestration readable from top to bottom. Extract detailed preparation and validation into
  clearly named helpers while keeping important state changes, event publication, and error or
  cancellation transitions visible at the point where they occur.
- Keep small private helpers and state beside their sole consumer when that helps readers follow
  the code. Judge extractions by whether they reduce what a reader must hold in mind and avoid
  unnecessary navigation; file length alone is not a reason to split or retain code.
- Use concrete names that describe purpose. Comments should explain intent, invariants, or subtle
  ordering constraints. Avoid generic utility modules, speculative abstractions, and wrappers that
  merely add another place to look.
- Preserve behavior during readability refactors, including public imports and observable ordering,
  unless a compatibility change is explicitly in scope. Keep unrelated behavior changes separate.

## Docstrings

- Use Google-style docstrings matching VS Code autoDocstring's default format for new or updated
  Python documentation. Start with a concise summary; include typed `Args`, `Returns` or `Yields`,
  and `Raises` sections where applicable. Omit empty sections and avoid placeholder descriptions.
- Explain meaningful contracts: state changes, ordering, cancellation, exception propagation, and
  the meaning of return values. Use `Yields` to describe emitted values for generator functions.
- For functions needing more explanation, place an `Examples` section immediately after `Returns`
  or `Yields`, before `Raises`. Use concrete examples that clarify behavior; simple helpers do not
  need examples. State any required caller-supplied setup and verify executable examples.
- Apply this convention incrementally within the requested scope. The `src/wisp/agent/loop/`
  package provides examples; do not expand a task into an unrelated documentation rewrite.

## Architecture boundaries

Wisp has one typed, event-driven runtime shared by every interface:

```text
CLI / JSONL-RPC / SDK adapters -> RPC command host -> CodingSession -> AgentHarness -> run_agent_loop
```

- Keep `run_agent_loop` provider-neutral and independent of persistence or frontend concerns.
- Keep the transcript across runs, user-message queues, and continuation policy in `AgentHarness`.
  The loop owns transient turn counters and continuation state within a single invocation; boundary
  hooks let the harness and session supply decisions without moving their policy into the loop.
- Keep durable session state, compaction orchestration, trust, and safety policy in `CodingSession`.
- Keep provider-native request, replay, continuation, and usage semantics inside the corresponding
  provider adapter. Shared helpers may manage typed lifecycle state but must not flatten meaningful
  provider differences.
- Keep CLI, TUI, RPC, and SDK behavior aligned through shared commands and typed `WispEvent` models;
  avoid interface-specific copies of runtime policy.
- Preserve append-only JSONL session semantics and backward-compatible event parsing when changing
  persisted schemas.

### Finding agent code

- Start with `src/wisp/agent/harness/runner.py` for transcript and queue behavior across runs,
  then `loop/runner.py` for execution within one run. Each package keeps configuration in `config.py`.
- `harness/boundaries.py` prepares boundary decisions and transcript replacements; `harness/runner.py`
  applies replacements when the next turn starts. The loop's `model_response.py`, `tool_execution.py`,
  and `continuation.py` contain its supporting mechanisms.
- `prompt/builder.py` assembles instructions in order; `prompt/instructions.py` holds core instruction
  text; `prompt/project_context.py` discovers trusted files and bounded Git/project context.
  `prompt/text_budget.py` applies shared character limits to context and tool guidance.
- `messages.py` defines message and compaction records and projects completion events. `history.py`
  normalizes provider history; `transcript_repair.py` orders tool results and repairs interruptions.
- `tool_contracts.py` defines executor protocols; `request_boundary.py` defines shared request hooks
  and decisions. `context_budget.py` estimates token budgets; `validation.py` validates runtime limits.
- Keep `wisp.agent.harness`, `wisp.agent.loop`, and `wisp.agent.prompt` as their public import surfaces.
  Within each package, import from defining modules. Use the current shared module names above;
  do not restore the removed `configuration.py`, `context.py`, `execution.py`, or `transcript.py`
  import shims. Import history helpers directly from `wisp.agent.history` and concrete session-entry
  models from `wisp.sessions`; do not restore the removed `wisp.agent.messages.SessionEntry` factory.

## Implementation conventions

- Target Python 3.12+ and preserve strict typing.
- Prefer small typed data structures and explicit lifecycle transitions over untyped dictionaries
  outside provider protocol boundaries.
- Preserve runtime compatibility for optional provider capabilities. Do not assume every third-party
  provider accepts newly added keyword arguments.
- Treat tool execution, approvals, protected paths, cancellation, retries, and process cleanup as
  security or reliability boundaries; add adversarial regression tests when changing them.
- Maintain stable ordering where order is observable, including prompt instructions, tool schemas,
  replay items, events, and persisted entries.
- When moving modules, update internal imports, architecture checks, and tests that patch module
  globals. Patch the binding used by the implementation, not a re-export on the public package.

## Verification

Run verification proportional to the change, starting with focused tests. For Python code changes,
the standard gates are:

```bash
uv run pytest <relevant test files>
uv run ruff format --check .
uv run ruff check .
uv run mypy
```

- Use the configured `uv run mypy`; do not substitute `uv run mypy .`, which can inspect unrelated
  reference modules outside the configured package scope.
- Run the full test suite for broad or cross-cutting changes when the environment supports it.
- Test observable behavior and consequential invariants. Reuse existing coverage and add regressions
  for concrete risks; documentation-only edits do not require application tests. After checks pass,
  repeat them only for new edits, failures, or unresolved concerns.
- Consult `.github/workflows/ci.yml` for additional gates relevant to the changed area. For Rust
  changes, run `cargo fmt --all --check`,
  `cargo clippy --workspace --all-targets --all-features -- -D warnings`, and
  `cargo test --workspace --all-features`. For Python/Rust handoff changes, include the TUI build and
  handoff smoke tests configured there; for packaging changes, include installed-package checks.
- For live protocol or event-model changes, run `uv run python -m wisp.rpc.protocol_schema --check`
  and the relevant schema/conformance tests. Preserve historical schema bundles and use the CI
  immutable-base check against the actual delivery base when preparing a PR.
- If a command is blocked by the environment or does not reach a terminal result, report that
  limitation explicitly instead of treating the gate as passed.
- For GitHub delivery, local checks are not final evidence: wait for terminal CI and inspect
  actionable review-thread state before recommending a merge.

## Git hygiene

- For GitHub delivery, use `github-pr-delivery` when available, or the installed GitHub delivery
  instructions relevant to the task. If no such skill is available, follow the explicit Git and CI
  rules in this file and report any verification gap; do not assume a missing skill was followed.
- Review `git status` before and after edits.
- Stage only files belonging to the requested task.
- Never discard or overwrite user changes to make the worktree clean.
- Update local `main` only with a verified fast-forward workflow; use ancestry checks rather than
  comparing object IDs alone.
