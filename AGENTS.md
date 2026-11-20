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

## Delegation and review

- The primary agent, whatever harness or model the user runs, owns design decisions,
  implementation, verification, and final integration. Respect an explicit user choice of model
  or agent. Map the roles below onto what your harness provides; role names and model selection
  belong in user-level configuration, not in this file.
- Delegate only bounded work: read-only code, dependency, or documentation research to an
  exploration subagent; independent implementation, debugging, or testing to a general-purpose
  one. Give each implementation assignment explicit file ownership so no two agents edit the same
  file, and keep decisions about the architecture boundaries below with the primary agent.
- Delegated agents follow this file. Restate task-specific constraints in the brief: scope,
  files not to touch, and that no delivery step (commit, push, PR) is authorized.
- For consequential changes to security, persistence, concurrency, cancellation, the
  tool-execution and approval boundaries listed under Implementation conventions, public
  contracts, or a large rewrite or cross-cutting change, get an independent read-only review from
  a fresh context against the requirements and the diff. The implementer's own review does not
  count. If no independent reviewer is available, say so in the handoff instead of implying a
  review happened.
- Verify every subagent or reviewer finding against source and tests before acting on it. Reject
  invalid findings with evidence. Raise meaningful readability issues, not style preferences.
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
- Preserve append-only JSONL session semantics. The session reader accepts only the current entry
  and event-envelope `schema_version`; it does not upgrade older files. Bump the stamp only when
  files written before a change would be misread or rejected (a new optional field needs no bump),
  and do not add readers for earlier versions.
- `WispEvent` payloads carry no per-event version. `schemas/live-rpc/` holds the one generated
  bundle describing the current release's wire contract; regenerate it after any command, event, or
  handshake model change. There are no historical bundles and the handshake carries no protocol
  version: compatibility comes from the same-release rule below. Do not reintroduce a per-event
  counter or a protocol number.
- The event schema lists every field the backend emits as required. Readers are more lenient in
  the same way on both sides: Python fills an omitted defaulted field from its model default, and
  the Rust frontend fills schema defaults before validating (`wisp-protocol` `event_defaults.rs`).
  Fields with a computed default (`timestamp`, a `default_factory`, a validator-derived value) stay
  required in Rust. Both reject unknown fields. `tests/fixtures/event_decoding_conformance.json`
  records the Python verdict for every one-field variant of each canonical event and the Rust
  conformance test must reach the same one; regenerate it after an event-model change. A defaulted
  field must satisfy its own constraints, since defaults are not validated on decode.
- A backend and a frontend must be the same Wisp release; both readers reject unknown fields, so
  any model change can break a mixed pair. Both frontends enforce this at connect time from the
  handshake's `backend_package_version`: the Rust TUI (`BackendVersionMismatch`) and the Python SDK
  transport (`RpcHandshakeError` with code `backend_version_mismatch`). Keep that check when adding
  a frontend. Supporting mixed releases would first require readers that tolerate unknown fields.

### Finding agent code

- In `src/wisp/agent/`, `harness/runner.py` owns the transcript and queues across runs. It applies
  replacements prepared by `harness/boundaries.py` at the next turn. Start in `loop/runner.py`
  for a single run. Both packages keep configuration in `config.py`.
- In the loop, follow a model response through `provider_request.py`, `provider_lifecycle.py`,
  `model_response.py`, and `response_projection.py`. For continuation and overflow, see
  `continuation.py` and `boundary_transitions.py`. For tool batches, start in `tool_execution.py`.
  The loop publishes turns through `turn_lifecycle.py`, which the harness and session also use
  when they must publish a terminal event themselves.
- Import events from `wisp.events`. Its `__init__.py` defines the `KnownWispEvent` union in schema
  order. Providers depend on `wisp.messages`, and the agent depends on providers. For history
  normalization see `messages/history.py`, and for interrupted tool calls see `agent/transcript_repair.py`.
- In `coding/session.py`, `CodingSession` coordinates the run and `_RunCompaction` owns the state
  shared by boundary and overflow hooks. See `compaction.py` for planning and `persistence.py`
  for branch-pinned writes. `coding/subagent.py` runs a second harness whose tool calls nest
  under the original `spawn_agent` call.
- For prompt order start in `coding/prompt/builder.py`. In that package, `project_context.py`
  loads bounded context and trusted instructions, `project_root.py` scopes trust, and
  `repository_status.py` supplies the Git snapshot the session reuses. Git calls share a deadline.
- In `sessions/`, start in `jsonl.py` for append and replay, and `file_io.py` for fsync ordering,
  locking, private modes, and tail recovery. Patch helpers in their defining modules. Store images
  in `sessions/images.py` before a message references them. `src/wisp/images.py` validates image
  headers without decoding the image. `ImagePart` belongs to `wisp.events`.
- `config/runtime.py` defines `WispConfig` and `config/settings.py` resolves layered settings.
  Keep `wisp.config` re-exports limited to the SDK runtime surface. Internal callers import from
  defining modules. Lower-level `wisp.mcp`, `wisp.retry`, and `wisp.validation` must not import
  `wisp.config`.
- `trust/records.py` owns project trust and the non-interactive override, `flow.py` the trust
  prompt, and `permissions.py` per-project permission modes. The package re-exports nothing.
  Import `wisp.events.PermissionMode` in `trust/permissions.py` only under `TYPE_CHECKING` to
  avoid a cycle during config initialization.
- CLI entry points live in `cli/application.py`. For RPC, follow `rpc/host.py` (transport),
  `coordinator.py` (queue and synchronization), then `execution.py` (dispatch). Import handlers
  from defining modules in `rpc/session/` or `rpc/handlers/`. `handlers/mcp.py` owns every MCP
  connect and restart. The host connects servers alongside the command queue after runtime
  construction. Prompt and compaction runs wait for connection. Directory changes that cross a
  project root use `configuration.py`'s `apply_project`.
- Keep `wisp.agent.harness`, `wisp.agent.loop`, and `wisp.coding.prompt` as their public import
  surfaces; prompt building is coding-layer code, so do not restore `wisp.agent.prompt`.
  Within each package, import from defining modules. Use the current shared module names above;
  do not restore the removed `configuration.py`, `context.py`, `execution.py`, or `transcript.py`
  import shims. Import message records from `wisp.messages`, history helpers directly from
  `wisp.messages.history`, and concrete session-entry models from `wisp.sessions`; do not restore
  `wisp.agent.messages`, `wisp.agent.history`, or the removed `SessionEntry` factory.
  The same rule applies outside `wisp.agent`: import tool implementations from their defining
  modules under `wisp.tools.files`, `wisp.tools.shell`, and `wisp.tools.search` (for example
  `wisp.tools.files.paths` or `wisp.tools.shell.supervisor`), and retry helpers from `wisp.retry`;
  do not restore the removed `wisp.tools.{process,file_ops,paths,process_manager,secure_fs}` or
  `wisp.providers.retry` shims.

### Finding TUI code

- Start in `rust/wisp-tui/src/lib.rs` for startup (`run_from_env`) and frame state (`LiveUi`).
  `event_loop.rs` coordinates input, transport, and paints. `terminal.rs` owns terminal modes,
  the panic hook, and synchronized frames. When `external_editor.rs` runs the user's editor,
  the event loop parks input with `InputGate` and suspends the terminal. `hyperlink.rs` builds
  the allowed URIs and paints OSC 8 links.
- `reducer.rs` and `reducer/` handle terminal-independent state transitions. `ui.rs` lays out
  the frame. `transcript.rs` owns transcript state and `transcript_view.rs` builds visible rows.
  Wire types come from `rust/wisp-protocol`, which validates against `schemas/live-rpc/`.
- Rendering is mostly hand-drawn: views build `Line`/`Span` text and paint it with `Paragraph`,
  `Block`, and `List` (in pickers), `Clear` for overlays, and a few direct `Buffer` writes. New
  views should follow the same pattern.
- Keep these invariants. The transcript draws no scrollbar (`visual_tests.rs` asserts it).
  `ui::sanitize_for_terminal` replaces every control and bidi character in untrusted text, which
  neutralizes OSC 0/8/52, CSI, and bidi overrides. Colours come from `theme.rs` palette tokens,
  mostly loaded from `theme_catalog.json`, and stay RGB while a frame is drawn; only
  `color_depth.rs` maps them to 256 or 16 colours, after rendering. Before drawing,
  `Palette::for_depth` moves background tiers apart for 256 colours and switches selections to
  reverse video at 16.
- Rendering tests draw `LiveUi` into ratatui's `TestBackend` and assert on the buffer:
  `visual_tests.rs` for screens and `overlay_tests.rs` for overlays, with more beside each view.
- For ratatui APIs and examples, use the crate versions in `Cargo.lock` under
  `~/.cargo/registry/src/*/`. Widget examples live in `ratatui-widgets`. Read CHANGELOG and
  BREAKING-CHANGES at the matching `ratatui-v<version>` GitHub tag.

## Implementation conventions

- Target Python 3.11+ and preserve strict typing. Without the `type` statement, name aliases with
  `typing_extensions.TypeAliasType` (as `type` did) and declare `TypeVar`s for generics.
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
  and the relevant schema/conformance tests.
- If a command is blocked by the environment or does not reach a terminal result, report that
  limitation explicitly instead of treating the gate as passed.
- For Astro source changes or branch switches, use `cd site && npm run verify:browser` to build
  before browser tests. `npm run test:browser` is test-only; reserve it for an unchanged build.
  CI may keep build and browser tests in separate ordered steps for timing visibility.
- Preserve a checked command's exit status when printing diagnostics, and read optional logs only
  if they exist. Do not let a successful `tail` hide a failed check or a missing log replace it.
- For GitHub delivery, local checks are not final evidence: wait for terminal CI and inspect
  actionable review-thread state before recommending a merge.

## Git hygiene

- Review `git status` before and after edits.
- Stage only files belonging to the requested task.
- Never discard or overwrite user changes to make the worktree clean.
- Update local `main` only with a verified fast-forward workflow; use ancestry checks rather than
  comparing object IDs alone.

## GitHub delivery

These rules apply only when the user asks for a commit, push, pull request, or merge.

- Start each change on its own branch from an up-to-date `main`, and confirm with
  `git merge-base --is-ancestor origin/main HEAD` before pushing.
- Stage explicit paths, never `git add .` when unrelated work exists. Check
  `git diff --cached --check`, and scan the staged diff for secrets, `.env` files, and local-only
  files before committing.
- Write commit subjects as `type(scope): summary`, matching the history. Use the body to explain why
  the change is needed, and add `Refs HY-123` when a Linear issue exists.
- Fill in `.github/PULL_REQUEST_TEMPLATE.md`. Describe the net diff against `main` rather than the
  branch's history, list the exact commands run and their results, and mark checks that were not
  run.
- After every push, wait until all checks reach a terminal state. Pending, queued, cancelled, or
  unexplained skipped checks are not passes; `CI passed` is the required status.
  For a status snapshot, use `gh pr view <PR> --json headRefOid,statusCheckRollup` and inspect the
  reported states and conclusions on the current head; successful JSON retrieval does not mean
  CI passed. `gh pr checks` documents exit code 8 as pending, not a failed check. Do not use
  `|| true` to hide errors, or infer success from missing checks.
- Codex reviews each pull request automatically. Verify each finding against the code. Fix a valid
  one narrowly, with a regression test where it applies, then reply with the fixing commit and
  resolve the thread only after the fix is pushed. Answer an invalid one with evidence instead of
  changing code to silence it. Do not comment `@codex review` to request a re-review after
  addressing findings; handle any further findings Codex posts on its own.
- A pull request is ready to merge when it is not a draft, GitHub reports it mergeable, every check
  on the current head passed, and no actionable review thread is unresolved.
- Never amend, rebase, force-push, merge, release, or delete branches unless the user asks.
