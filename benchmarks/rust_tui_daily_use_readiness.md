# Rust TUI daily-use readiness (#467)

This inventory covers the daily-use changes on top of `a7cc3a3` (#548), dated 2026-09-12.
It records source and regression evidence, not comparative latency or a supported rollout.
Textual remains the supported default; Rust remains experimental source-build opt-in on macOS/Linux.

## Acceptance inventory

| #467 criterion | Evidence in this tree | Disposition |
|---|---|---|
| Backend-confirmed model/provider/effort | `lib.rs`, `reducer.rs`, `model_picker.rs`; shared model transition traces and `tests/test_rpc_configuration.py` | Existing #538 behavior retained; no local catalog or provider policy added |
| Masked connection secrets and defined lifecycle | `connection_panel.rs::api_key_is_masked_capped_and_redacted`, live connection tests in `lib.rs`, `overlay_tests.rs::connection_refresh_cannot_retarget_disconnect_before_redraw_and_secret_d_is_literal` | Only defined secret commands; auth hygiene and adversarial lifecycle completion remains #468 |
| Protected file paths and policy generations | `tests/test_rpc_project_files.py`, `file_picker_tests.rs::resize_changed_query_and_policy_invalidation_retire_the_rendered_selection` and `trust_and_modals_preempt_files_without_leaking_input_or_retained_paths` | Python owns filtering; Rust retires stale snapshots/selections |
| Typed skills, commands, MCP | `command_tests.rs`, `discovery.rs`, protocol catalogs and shared transition traces | Existing #539/#541 behavior; MCP reconnect controls remain outside this slice |
| Resolved help and reachable recovery | `keybindings.rs`, `keybinding_tests.rs`, `tests/test_settings.py`, `tests/test_rust_tui_launcher.py` | Stable action IDs, replacement/unbinding/conflict rules, user-only settings, fixed Ctrl+G/Escape/Ctrl+C and decision denial |
| Resize, paste, wide/combining Unicode, multiline, focus | `prompt_editor.rs` editor/fold tests, `ui.rs` projection tests, `mouse_tests.rs`, `tests/test_rust_tui_readiness.py` | Raw text authoritative; compact folds expand before hidden edits; exact raw launcher submission tested |
| Narrow/wide layouts and visible decisions | `overlay_tests.rs::popup_clear_is_opaque_and_geometry_stays_bounded`, positive-decision visibility tests in `lib.rs`, `keybinding_tests.rs` help at 30×8/80×24/160×40 and maximum aliases | Below 30×8, decisions cannot activate unseen controls; help scrolls visual rows at the minimum supported size |
| Transcript remains mounted under overlays | `overlay_tests.rs::every_overlay_preserves_background_cells_draft_and_viewport_on_close`, `streaming_updates_background_without_changing_scroll_intent_or_eating_input` | Existing paint ordering preserved; contextual help uses the same conversation-first rendering |
| Focused overlay input and recovery | `overlay_tests.rs`, `keybinding_tests.rs` theme/completion/browse precedence and help cancellation tests | Fixed local controls precede custom actions; inherited global Ctrl+T behavior retained |
| Trusted-project surface refresh | `tests/test_rpc_configuration.py`, `tests/test_rpc_project_files.py::test_host_trust_transition_invalidates_even_when_configuration_is_equal`, shared auth/model/file traces and Rust stale-selection tests | Existing backend authority retained; project settings cannot replace user keybindings |
| Preferences cannot change runtime/safety policy | `tests/test_settings.py`, `tests/test_rust_tui_launcher.py`, backend `process.rs` environment removal | Private launch snapshot only; no event/RPC/persistence schema changes |
| No undocumented critical workflow | Updated `site/architecture/rust-tui-boundary.md` parity matrix and `site/guide/tui.md` | External update instructions are the explicit #467 alternative; remaining stage-3 gates are #468/#469 |

## Binding ownership (#445)

The action inventory and user configuration examples live in the TUI guide. Rust's `Action::ALL`
metadata owns identifiers, descriptions and help labels; there is no second runtime-policy settings
system. User settings use the existing Python loader. The launcher replaces inherited private binding
JSON, and the backend child removes it. Restart is required. Malformed configurations fall back as
one unit while unrelated valid settings survive.

Configurable actions cover composer submission/newline, queue restore, history, theme and transcript
navigation. Recovery, decision controls, local picker/completion navigation and basic editor controls
remain fixed. Default modifier aliases are retained but disappear with their action's explicit
override. The future settings screen (#112) can consume the same typed action metadata; implementing
that screen is not part of this change.

## Paste and update differences

Pastes over 2,000 Unicode scalar values fold after ordinary newline/control normalization. Raw text
remains subject to 1 MiB / 10,000 lines. The editor retains at most 64 folds. Live queue and transcript
presentations each have a 32-entry / 4-MiB budget; oldest display metadata is discarded without
altering raw content. Tests cover duplicate queue text, FIFO matching, pending/accepted budgets,
failed/abandoned queues, restoration, historical replay, and cache invalidation on metadata eviction.
The PTY scenario checks exact persisted Unicode multiline text through the real Python launcher.

`/update [check|install]` opens scrollable external instructions and preserves the draft. It performs
no network check, installation, quit or restart. The user quits, checks with `wisp update --check`,
uses `wisp update` for eligible uv tool installs or the source development workflow, and rebuilds or
selects a matching Rust binary before relaunch. Automatic notices, artifact integrity, install,
rollback, and coordinated restart remain #469.

## Verification and remaining gates

Local terminal results: Cargo formatting and clippy passed; full workspace tests passed 573 cases.
Python formatting/lint/mypy, generated schema/theme checks (including immutable base `a7cc3a3`),
documentation build, and 16 renderer/compatibility documentation tests passed. Existing launcher
smoke/theme/mouse PTYs passed all 25 cases; the four new readiness PTYs passed after correcting the
large-paste assertion to scroll back past the fake assistant's long response before checking the
compact user echo. CI includes
`tests/test_rust_tui_readiness.py` alongside existing smoke/theme/mouse scenarios on Linux and macOS.
Source inventory alone is not evidence that every platform check passed; use the PR's current-head
CI results for that claim.

Local full Python run: 5,468 passed, 21 skipped, one process-timeout cleanup failure
(`test_bash_tool_reports_timeout_and_kills_child_processes`). The isolated retry, together with
settings/launcher tests, passed all 82 cases. This is a recorded test instability, not a claim of a
clean full-suite run.

#468 still owns broad fuzzing, backpressure, terminal/secret hygiene and fault recovery. #469 still
owns distribution and install/update/rollback. Transcript search and built-in selection/copy remain
unavailable and documented; mouse is opt-in. Optional decorative work, MCP reconnect controls and
other #237 enhancements are not automatic parity blockers. Comparative dual-renderer PTY latency,
supported opt-in feedback, accessibility/support/rollback assessment and an explicit new decision
remain required before a default switch or Textual deprecation. The September 3 benchmark evidence
is unchanged.
